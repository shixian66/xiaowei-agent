"""慢查询 Evidence 到模型请求的唯一 capability-aware 投影边界。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, TypeAlias, TypeGuard

from pydantic import ValidationError

from xiaowei_agent.application.model_ports import (
    ADVISORY_OUTPUT_TOKEN_LIMIT,
    ModelPortError,
    SlowQueryAdvisoryPort,
    fallback_code_for_model_error,
    validate_advisory_output_tokens,
)
from xiaowei_agent.capabilities.specs import (
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    OP_LIST,
    SLOW_QUERY_SURFACE,
)
from xiaowei_agent.contracts import (
    MAX_ADVISORY_ROWS,
    MAX_MODEL_TEXT_CHARACTERS,
    AdvisoryModelResult,
    AnswerabilityVerdict,
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalSource,
    JsonScalar,
    ModelCallKind,
    ModelCallObservation,
    ModelErrorCode,
    ModelFallbackCode,
    ModelInvocationProfile,
    ModelUsage,
    SlowQueryAdvisoryRequest,
    TaskOutcome,
    TaskStatus,
    content_digest,
)
from xiaowei_agent.contracts.evidence import evidence_id
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.persistence.model_artifacts import (
    AdvisoryArtifactCandidate,
    ModelArtifactConflictError,
    ModelArtifactStore,
    StoredModelAdvisory,
)
from xiaowei_agent.persistence.rows import dump_contract
from xiaowei_agent.persistence.store import TaskAttemptGrant
from xiaowei_agent.planning import canonical_json
from xiaowei_agent.redaction import scrub_text

_TEXT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"queryId", "timestamp", "state", "errorCode", "db", "user"}
)
_NUMBER_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "queryTime",
        "scanRows",
        "returnRows",
        "scanBytes",
        "memCostBytes",
        "pendingTimeMs",
        "cpuCostNs",
    }
)
_stage_timeout = asyncio.timeout
_SIGNED_BIGINT_MAX: Final[int] = 2**63 - 1
MonotonicClock: TypeAlias = Callable[[], float]


@dataclass(frozen=True)
class AdvisoryStageResult:
    result: AdvisoryModelResult | None
    observation: ModelCallObservation


@dataclass(frozen=True)
class AcceptedAdvisoryResult:
    request: SlowQueryAdvisoryRequest | None
    artifact: StoredModelAdvisory | None
    observation: ModelCallObservation | None


def _elapsed_ms(started: float, monotonic: MonotonicClock) -> int:
    return min(_SIGNED_BIGINT_MAX, max(0, int((monotonic() - started) * 1_000)))


def _advisory_observation(
    *,
    started: float,
    request_count: int,
    usage: ModelUsage | None = None,
    fallback_code: ModelFallbackCode | None,
    monotonic: MonotonicClock,
) -> ModelCallObservation:
    safe_usage = usage or ModelUsage()
    return ModelCallObservation(
        call_kind=ModelCallKind.ADVISORY,
        elapsed_ms=_elapsed_ms(started, monotonic),
        request_count=request_count,
        input_tokens=safe_usage.input_tokens,
        output_tokens=safe_usage.output_tokens,
        fallback_code=fallback_code,
    )


def _digest(payload: object) -> str:
    return content_digest(canonical_json(payload).decode("utf-8"))


def advisory_input_digest(
    request: SlowQueryAdvisoryRequest,
    *,
    plan: ExecutionPlan,
    profile: ModelInvocationProfile,
) -> str:
    """绑定 typed 行、surface/evidence revision 与当前 capability/profile。"""
    from xiaowei_agent.capabilities.specs import SLOW_QUERY_SPEC

    return _digest(
        {
            "request": dump_contract(request),
            "capability_id": plan.capability_id,
            "capability_version": plan.capability_version,
            "surface_id": SLOW_QUERY_SURFACE.surface_id,
            "surface_columns": SLOW_QUERY_SURFACE.allowed_columns,
            "evidence_contract": SLOW_QUERY_SPEC.evidence_contract,
            "provider": profile.provider,
            "model": profile.model,
            "origin": profile.origin,
            "api_version": profile.api_version,
            "prompt_revision": profile.advisory_prompt_revision,
            "schema_revision": profile.advisory_schema_revision,
        }
    )


def advisory_result_digest(result: AdvisoryModelResult) -> str:
    return _digest({"advisory": dump_contract(result.advisory)})


def advisory_artifact_identity_matches(
    artifact: StoredModelAdvisory, *, profile: ModelInvocationProfile
) -> bool:
    """复验持久化 provider/profile 身份，防止元数据与摘要各自漂移。"""
    return (
        artifact.origin == "model"
        and artifact.provider == profile.provider
        and artifact.model == profile.model
        and artifact.provider_origin == profile.origin
        and artifact.prompt_revision == profile.advisory_prompt_revision
        and artifact.schema_revision == profile.advisory_schema_revision
    )


async def request_model_advisory(
    *,
    request: SlowQueryAdvisoryRequest,
    model: SlowQueryAdvisoryPort | None,
    profile: ModelInvocationProfile,
    plan: ExecutionPlan,
    monotonic: MonotonicClock = time.monotonic,
) -> AdvisoryStageResult:
    """在当前 plan 与固定 ceiling 的较小值内请求一次 advisory。"""
    started = monotonic()
    if model is None:
        return AdvisoryStageResult(
            result=None,
            observation=_advisory_observation(
                started=started,
                request_count=0,
                fallback_code=ModelFallbackCode.DISABLED,
                monotonic=monotonic,
            ),
        )
    output_tokens = validate_advisory_output_tokens(
        min(ADVISORY_OUTPUT_TOKEN_LIMIT, plan.budget.max_model_tokens)
    )
    try:
        async with _stage_timeout(profile.advisory_timeout_seconds):
            result = await model.generate_advisory(
                request, max_output_tokens=output_tokens
            )
            if any(
                scrub_text(value) != value
                for value in model_text_values(result.advisory)
            ):
                raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
    except TimeoutError:
        fallback = ModelFallbackCode.APPLICATION_TIMEOUT
    except ModelPortError as exc:
        fallback = fallback_code_for_model_error(exc.code)
    else:
        return AdvisoryStageResult(
            result=result,
            observation=_advisory_observation(
                started=started,
                request_count=1,
                usage=result.usage,
                fallback_code=None,
                monotonic=monotonic,
            ),
        )
    return AdvisoryStageResult(
        result=None,
        observation=_advisory_observation(
            started=started,
            request_count=1,
            fallback_code=fallback,
            monotonic=monotonic,
        ),
    )


async def load_or_accept_advisory(
    *,
    grant: TaskAttemptGrant,
    task_id: str,
    plan: ExecutionPlan,
    outcome: TaskOutcome,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    projector: Callable[..., SlowQueryAdvisoryRequest | None] | None,
    model: SlowQueryAdvisoryPort | None,
    profile: ModelInvocationProfile,
    artifacts: ModelArtifactStore,
    monotonic: MonotonicClock = time.monotonic,
) -> AcceptedAdvisoryResult:
    """只对 projector 接受的成功证据调用一次模型并在终态前保存。"""
    if projector is None:
        return AcceptedAdvisoryResult(request=None, artifact=None, observation=None)
    request = projector(
        task_id=task_id,
        plan=plan,
        outcome=outcome,
        evidences=evidences,
        verdict=verdict,
    )
    if request is None:
        return AcceptedAdvisoryResult(request=None, artifact=None, observation=None)
    input_digest = advisory_input_digest(request, plan=plan, profile=profile)
    existing = await artifacts.load_advisory(task_id=task_id)
    if existing is not None:
        existing_result = AdvisoryModelResult(
            advisory=existing.advisory,
            usage=existing.usage,
        )
        if (
            not advisory_artifact_identity_matches(existing, profile=profile)
            or existing.input_digest != input_digest
            or existing.result_digest != advisory_result_digest(existing_result)
        ):
            raise ModelArtifactConflictError(
                "stored model advisory does not match rebuilt input",
                task_id=task_id,
            )
        return AcceptedAdvisoryResult(
            request=request, artifact=existing, observation=None
        )

    stage = await request_model_advisory(
        request=request,
        model=model,
        profile=profile,
        plan=plan,
        monotonic=monotonic,
    )
    if stage.result is None:
        return AcceptedAdvisoryResult(
            request=request,
            artifact=None,
            observation=stage.observation,
        )
    candidate = AdvisoryArtifactCandidate(
        advisory=stage.result.advisory,
        provider=profile.provider,
        model=profile.model,
        provider_origin=profile.origin,
        prompt_revision=profile.advisory_prompt_revision,
        schema_revision=profile.advisory_schema_revision,
        input_digest=input_digest,
        result_digest=advisory_result_digest(stage.result),
        usage=stage.result.usage,
    )
    artifact = await artifacts.save_advisory(grant=grant, candidate=candidate)
    return AcceptedAdvisoryResult(
        request=request,
        artifact=artifact,
        observation=stage.observation,
    )


def _valid_text(value: object) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    if len(value) > MAX_MODEL_TEXT_CHARACTERS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _project_row(
    row: Mapping[str, object], *, scrub: bool
) -> dict[str, JsonScalar] | None:
    columns = SLOW_QUERY_SURFACE.allowed_columns
    if set(row) != set(columns):
        return None
    projected: dict[str, JsonScalar] = {}
    for column in columns:
        value = row[column]
        if column in _TEXT_COLUMNS:
            if not _valid_text(value):
                return None
            projected[column] = scrub_text(value) if scrub else value
        elif column in _NUMBER_COLUMNS:
            if not _valid_number(value):
                return None
            projected[column] = value
        else:  # pragma: no cover - surface pairing test makes this unreachable
            return None
    return projected


def project_slow_query_advisory_request(
    *,
    task_id: str,
    plan: ExecutionPlan,
    outcome: TaskOutcome,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
) -> SlowQueryAdvisoryRequest | None:
    """整批校验后返回最多 20 行的固定出站 DTO；任何漂移都拒绝。"""
    if (
        outcome.task_id != task_id
        or outcome.status is not TaskStatus.SUCCEEDED
        or not verdict.sufficient
        or (plan.capability_id, plan.capability_version)
        != (CAPABILITY_ID, CAPABILITY_VERSION)
    ):
        return None
    steps = tuple(step for step in plan.steps if step.operation == OP_LIST)
    if len(steps) != 1:
        return None
    expected_evidence_id = evidence_id(task_id=task_id, step_id=steps[0].step_id)
    matching = tuple(item for item in evidences if item.evidence_id == expected_evidence_id)
    if (
        len(matching) != 1
        or expected_evidence_id not in outcome.evidence_refs
        or matching[0].source_kind is not ExternalSource.TOOL
        or (matching[0].capability_id, matching[0].capability_version)
        != (plan.capability_id, plan.capability_version)
        or not matching[0].facts
    ):
        return None
    evidence = matching[0]
    raw_rows: list[dict[str, JsonScalar]] = []
    for raw in evidence.facts[:MAX_ADVISORY_ROWS]:
        projected = _project_row(raw, scrub=False)
        if projected is None:
            return None
        raw_rows.append(projected)
    # 原始 typed 批次先受独立总量上限约束；脱敏后再构造一次最终 DTO，边界不依赖
    # scrub_text 当前只缩短文本这一实现细节。
    try:
        SlowQueryAdvisoryRequest(
            rows=tuple(raw_rows),
            sampled=evidence.sampled or len(evidence.facts) > MAX_ADVISORY_ROWS,
        )
        scrubbed_rows = tuple(
            _project_row(row, scrub=True) for row in raw_rows
        )
        if any(row is None for row in scrubbed_rows):  # pragma: no cover - same rows
            return None
        return SlowQueryAdvisoryRequest(
            rows=tuple(row for row in scrubbed_rows if row is not None),
            sampled=evidence.sampled or len(evidence.facts) > MAX_ADVISORY_ROWS,
        )
    except (ValidationError, ValueError):
        return None


__all__ = [
    "AcceptedAdvisoryResult",
    "AdvisoryStageResult",
    "advisory_artifact_identity_matches",
    "advisory_input_digest",
    "advisory_result_digest",
    "load_or_accept_advisory",
    "project_slow_query_advisory_request",
    "request_model_advisory",
]

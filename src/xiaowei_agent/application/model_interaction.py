"""无执行权的模型 interaction 分类预算与确定性降级。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from pydantic import ValidationError

from xiaowei_agent.application.model_ports import (
    InteractionClassifierPort,
    ModelPortError,
    fallback_code_for_model_error,
)
from xiaowei_agent.capabilities.intent import IntentInterpreter
from xiaowei_agent.contracts import (
    MAX_MODEL_TEXT_CHARACTERS,
    ClarificationContext,
    IntentSource,
    InteractionClassifierRequest,
    InteractionDraft,
    InteractionKind,
    InteractionSource,
    ModelCallKind,
    ModelCallObservation,
    ModelFallbackCode,
    ModelInvocationProfile,
    ModelUsage,
    ProviderIntentResponse,
    ProviderIntentSlots,
    RequestContext,
    RequestEnvelope,
    content_digest,
)
from xiaowei_agent.contracts.intent import UNKNOWN_INTENT
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.persistence.model_artifacts import (
    AcceptedInteractionArtifact,
    InteractionArtifactCandidate,
    ModelArtifactConflictError,
    ModelArtifactStore,
)
from xiaowei_agent.persistence.rows import dump_contract
from xiaowei_agent.persistence.store import TaskAttemptGrant
from xiaowei_agent.planning import canonical_json
from xiaowei_agent.redaction import scrub_text

InteractionFallback: TypeAlias = Callable[[], InteractionDraft]

_stage_timeout = asyncio.timeout
_SIGNED_BIGINT_MAX: Final[int] = 2**63 - 1
RULE_INTERACTION_PROMPT_REVISION: Final[str] = (
    "rule-interaction-prompt-not-applicable-v1"
)
RULE_INTERACTION_SCHEMA_REVISION: Final[str] = "rule-interaction-schema-v1"


@dataclass(frozen=True)
class InteractionStageResult:
    """最终交给 Router 的候选及安全模型阶段元数据。"""

    draft: InteractionDraft
    usage: ModelUsage
    observation: ModelCallObservation


@dataclass(frozen=True)
class AcceptedInteractionResult:
    """持久化 winner 与本次新调用产生的 trace 元数据。"""

    artifact: AcceptedInteractionArtifact
    observation: ModelCallObservation | None


class ModelInputRejectedError(ValueError):
    """Typed 模型输入超出本地工作上限；错误文本不回显输入。"""


def _require_model_text(value: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_MODEL_TEXT_CHARACTERS:
        raise ModelInputRejectedError("model input text exceeds its limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ModelInputRejectedError("model input text is not valid UTF-8") from None


def build_interaction_classifier_request(
    *,
    user_text: str,
    clarification: ClarificationContext | None = None,
) -> InteractionClassifierRequest:
    """先验原始边界，再脱敏并复验最终 typed request。"""
    _require_model_text(user_text)
    try:
        return InteractionClassifierRequest(
            user_text=scrub_text(user_text),
            clarification=clarification,
        )
    except ValidationError:
        raise ModelInputRejectedError(
            "model input exceeds the serialized request limit"
        ) from None


def _elapsed_ms(started: float, monotonic: Callable[[], float]) -> int:
    return min(_SIGNED_BIGINT_MAX, max(0, int((monotonic() - started) * 1_000)))


def _observation(
    *,
    started: float,
    monotonic: Callable[[], float],
    request_count: int,
    usage: ModelUsage | None = None,
    fallback_code: ModelFallbackCode | None,
) -> ModelCallObservation:
    safe_usage = usage or ModelUsage()
    return ModelCallObservation(
        call_kind=ModelCallKind.INTERACTION,
        elapsed_ms=_elapsed_ms(started, monotonic),
        request_count=request_count,
        input_tokens=safe_usage.input_tokens,
        output_tokens=safe_usage.output_tokens,
        fallback_code=fallback_code,
    )


def _accepted_model_draft(draft: InteractionDraft) -> bool:
    if draft.source is not InteractionSource.MODEL or any(
        scrub_text(value) != value for value in model_text_values(draft)
    ):
        return False
    if draft.capability_draft is None:
        return True
    try:
        ProviderIntentResponse(
            intent=draft.capability_draft.intent,
            slots=ProviderIntentSlots.model_validate(
                dict(draft.capability_draft.slots)
            ),
            missing=draft.capability_draft.missing,
            confidence=draft.capability_draft.confidence,
        )
    except ValidationError:
        return False
    return draft.capability_draft.source is IntentSource.MODEL


def _digest(payload: object) -> str:
    return content_digest(canonical_json(payload).decode("utf-8"))


def _opaque_text_digest(value: str) -> str:
    safe = scrub_text(value)
    return hashlib.sha256(safe.encode("utf-8", errors="surrogatepass")).hexdigest()


def interaction_input_digest(
    request: InteractionClassifierRequest,
    *,
    profile: ModelInvocationProfile,
    origin: Literal["model", "rule"] = "model",
) -> str:
    """Interaction 结果绑定当前轮输入、澄清上下文与 provider identity。"""
    payload: dict[str, object] = {
        "request": dump_contract(request),
        "interaction_origin": origin,
    }
    if origin == "model":
        payload["model_identity"] = {
            "provider": profile.provider,
            "model": profile.model,
            "origin": profile.origin,
            "api_version": profile.api_version,
            "prompt_revision": profile.interaction_prompt_revision,
            "schema_revision": profile.interaction_schema_revision,
        }
    return _digest(payload)


def _rejected_interaction_input_digest(*, envelope: RequestEnvelope) -> str:
    payload: dict[str, object] = {
        "rejected_input": True,
        "interaction_origin": "rule",
        "user_text_digest": _opaque_text_digest(envelope.text),
    }
    return _digest(payload)


def interaction_result_digest(draft: InteractionDraft) -> str:
    return _digest({"draft": dump_contract(draft)})


def interaction_artifact_identity_matches(
    artifact: AcceptedInteractionArtifact, *, profile: ModelInvocationProfile
) -> bool:
    """复验持久化 identity；不能只相信可与元数据分别漂移的 digest。"""
    if artifact.origin == "model":
        return (
            artifact.provider,
            artifact.model,
            artifact.provider_origin,
            artifact.prompt_revision,
            artifact.schema_revision,
        ) == (
            profile.provider,
            profile.model,
            profile.origin,
            profile.interaction_prompt_revision,
            profile.interaction_schema_revision,
        )
    return (
        artifact.origin == "rule"
        and artifact.provider is None
        and artifact.model is None
        and artifact.provider_origin is None
    )


def rule_interaction_fallback(
    *, envelope: RequestEnvelope, context: RequestContext, interpreter: IntentInterpreter
) -> InteractionDraft:
    """旧规则解释器只在唯一识别 capability 时产生 capability route。"""
    capability = interpreter.interpret(text=envelope.text, context=context)
    if capability.intent == UNKNOWN_INTENT:
        return InteractionDraft(
            proposed_kind=InteractionKind.UNKNOWN,
            capability_draft=None,
            confidence=0.0,
            source=InteractionSource.RULE,
        )
    return InteractionDraft(
        proposed_kind=InteractionKind.CAPABILITY_REQUEST,
        capability_draft=capability,
        confidence=capability.confidence,
        source=InteractionSource.RULE,
    )


async def request_interaction_draft(
    *,
    request: InteractionClassifierRequest,
    model: InteractionClassifierPort | None,
    profile: ModelInvocationProfile,
    fallback: InteractionFallback,
    monotonic: Callable[[], float] = time.monotonic,
) -> InteractionStageResult:
    """在固定共享预算内分类 interaction；普通失败降级，调用方取消原样传播。"""
    started = monotonic()
    if model is None:
        return InteractionStageResult(
            draft=fallback(),
            usage=ModelUsage(),
            observation=_observation(
                started=started,
                monotonic=monotonic,
                request_count=0,
                fallback_code=ModelFallbackCode.DISABLED,
            ),
        )

    request_count = 0
    failure: ModelFallbackCode | None = None
    result = None
    try:
        async with _stage_timeout(profile.interaction_timeout_seconds):
            request_count = 1
            result = await model.classify(request)
            if not _accepted_model_draft(result.draft):
                failure = ModelFallbackCode.INVALID_RESPONSE
                result = None
    except TimeoutError:
        failure = ModelFallbackCode.APPLICATION_TIMEOUT
    except ModelPortError as exc:
        failure = fallback_code_for_model_error(exc.code)

    if result is not None:
        return InteractionStageResult(
            draft=result.draft,
            usage=result.usage,
            observation=_observation(
                started=started,
                monotonic=monotonic,
                request_count=request_count,
                usage=result.usage,
                fallback_code=None,
            ),
        )
    return InteractionStageResult(
        draft=fallback(),
        usage=ModelUsage(),
        observation=_observation(
            started=started,
            monotonic=monotonic,
            request_count=request_count,
            fallback_code=failure or ModelFallbackCode.UNAVAILABLE,
        ),
    )


async def load_or_accept_interaction(
    *,
    grant: TaskAttemptGrant,
    envelope: RequestEnvelope,
    context: RequestContext,
    interpreter: IntentInterpreter,
    model: InteractionClassifierPort | None,
    profile: ModelInvocationProfile,
    artifacts: ModelArtifactStore,
    monotonic: Callable[[], float] = time.monotonic,
) -> AcceptedInteractionResult:
    """重建输入、复用匹配 winner，或调用模型并在 Router 前保存 winner。"""
    request: InteractionClassifierRequest | None
    try:
        built_request = build_interaction_classifier_request(user_text=envelope.text)
    except ModelInputRejectedError:
        request = None
    else:
        request = built_request

    existing = await artifacts.load_interaction(task_id=grant.task_id)
    if existing is not None:
        input_digest = (
            _rejected_interaction_input_digest(envelope=envelope)
            if request is None
            else interaction_input_digest(
                request,
                profile=profile,
                origin=existing.origin,
            )
        )
        if (
            (request is None and existing.origin != "rule")
            or not interaction_artifact_identity_matches(existing, profile=profile)
            or existing.input_digest != input_digest
            or existing.result_digest != interaction_result_digest(existing.draft)
        ):
            raise ModelArtifactConflictError(
                "stored interaction artifact does not match rebuilt input",
                task_id=grant.task_id,
            )
        return AcceptedInteractionResult(artifact=existing, observation=None)

    def fallback() -> InteractionDraft:
        return rule_interaction_fallback(
            envelope=envelope,
            context=context,
            interpreter=interpreter,
        )

    if request is None:
        started = monotonic()
        stage = InteractionStageResult(
            draft=fallback(),
            usage=ModelUsage(),
            observation=_observation(
                started=started,
                monotonic=monotonic,
                request_count=0,
                fallback_code=ModelFallbackCode.INPUT_REJECTED,
            ),
        )
    else:
        stage = await request_interaction_draft(
            request=request,
            model=model,
            profile=profile,
            fallback=fallback,
            monotonic=monotonic,
        )

    is_model = stage.draft.source is InteractionSource.MODEL
    artifact_origin: Literal["model", "rule"] = "model" if is_model else "rule"
    input_digest = (
        _rejected_interaction_input_digest(envelope=envelope)
        if request is None
        else interaction_input_digest(
            request,
            profile=profile,
            origin=artifact_origin,
        )
    )
    candidate = InteractionArtifactCandidate(
        draft=stage.draft,
        origin=artifact_origin,
        provider=profile.provider if is_model else None,
        model=profile.model if is_model else None,
        provider_origin=profile.origin if is_model else None,
        prompt_revision=(
            profile.interaction_prompt_revision
            if is_model
            else RULE_INTERACTION_PROMPT_REVISION
        ),
        schema_revision=(
            profile.interaction_schema_revision
            if is_model
            else RULE_INTERACTION_SCHEMA_REVISION
        ),
        input_digest=input_digest,
        result_digest=interaction_result_digest(stage.draft),
        usage=stage.usage,
    )
    artifact = await artifacts.save_interaction(grant=grant, candidate=candidate)
    return AcceptedInteractionResult(artifact=artifact, observation=stage.observation)


__all__ = [
    "RULE_INTERACTION_PROMPT_REVISION",
    "RULE_INTERACTION_SCHEMA_REVISION",
    "AcceptedInteractionResult",
    "InteractionStageResult",
    "ModelInputRejectedError",
    "build_interaction_classifier_request",
    "interaction_artifact_identity_matches",
    "interaction_input_digest",
    "interaction_result_digest",
    "load_or_accept_interaction",
    "request_interaction_draft",
    "rule_interaction_fallback",
]

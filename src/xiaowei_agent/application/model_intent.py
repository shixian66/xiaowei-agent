"""无执行权的模型 intent 调用预算与确定性降级。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from pydantic import ValidationError

from xiaowei_agent.application.model_ports import (
    INTENT_RETRYABLE_ERROR_CODES,
    IntentModelPort,
    ModelPortError,
    fallback_code_for_model_error,
)
from xiaowei_agent.capabilities.intent import IntentInterpreter
from xiaowei_agent.contracts import (
    MAX_MODEL_HISTORY_CHARACTERS,
    MAX_MODEL_HISTORY_ITEMS,
    MAX_MODEL_TEXT_CHARACTERS,
    IntentDraft,
    IntentSource,
    ModelCallKind,
    ModelCallObservation,
    ModelFallbackCode,
    ModelIntentRequest,
    ModelInvocationProfile,
    ModelUsage,
    ProviderIntentResponse,
    ProviderIntentSlots,
    RequestContext,
    RequestEnvelope,
    content_digest,
)
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.persistence.model_artifacts import (
    AcceptedIntentArtifact,
    IntentArtifactCandidate,
    ModelArtifactConflictError,
    ModelArtifactStore,
)
from xiaowei_agent.persistence.rows import dump_contract
from xiaowei_agent.persistence.store import TaskAttemptGrant
from xiaowei_agent.planning import canonical_json
from xiaowei_agent.redaction import scrub_text

MonotonicClock: TypeAlias = Callable[[], float]
AsyncSleep: TypeAlias = Callable[[float], Awaitable[None]]
IntentFallback: TypeAlias = Callable[[], IntentDraft]

# 保留为模块别名，让测试可以缩短本地 stage budget，而不把可配置 timeout 暴露到
# 生产构造面。真实调用始终取固定 ModelInvocationProfile 的 60 秒。
_stage_timeout = asyncio.timeout
_SIGNED_BIGINT_MAX = 2**63 - 1
RULE_INTENT_PROMPT_REVISION: Final[str] = "rule-intent-prompt-not-applicable-v1"
RULE_INTENT_SCHEMA_REVISION: Final[str] = "rule-intent-schema-v1"


@dataclass(frozen=True)
class IntentStageResult:
    """最终可交给 Resolver 的草案及安全模型阶段元数据。"""

    draft: IntentDraft
    usage: ModelUsage
    observation: ModelCallObservation


@dataclass(frozen=True)
class AcceptedIntentResult:
    """持久化 winner 与本次新调用产生的 trace 元数据。"""

    artifact: AcceptedIntentArtifact
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


def build_model_intent_request(
    *,
    user_text: str,
    history: tuple[str, ...],
    context_truncated: bool = False,
) -> ModelIntentRequest:
    """先验原始边界，再整轮截断、脱敏并复验最终 typed request。"""
    _require_model_text(user_text)
    truncated = context_truncated or len(history) > MAX_MODEL_HISTORY_ITEMS
    retained = list(history[-MAX_MODEL_HISTORY_ITEMS:])
    for item in retained:
        _require_model_text(item)
    while sum(map(len, retained)) > MAX_MODEL_HISTORY_CHARACTERS:
        retained.pop(0)
        truncated = True
    scrubbed_user = scrub_text(user_text)
    scrubbed_history = [scrub_text(item) for item in retained]
    while True:
        try:
            return ModelIntentRequest(
                user_text=scrubbed_user,
                history=tuple(scrubbed_history),
                context_truncated=truncated,
            )
        except ValidationError:
            if not scrubbed_history:
                raise ModelInputRejectedError(
                    "model input exceeds the serialized request limit"
                ) from None
            scrubbed_history.pop(0)
            truncated = True


def _elapsed_ms(started: float, monotonic: MonotonicClock) -> int:
    return min(_SIGNED_BIGINT_MAX, max(0, int((monotonic() - started) * 1_000)))


def _observation(
    *,
    started: float,
    monotonic: MonotonicClock,
    request_count: int,
    usage: ModelUsage | None = None,
    fallback_code: ModelFallbackCode | None,
) -> ModelCallObservation:
    safe_usage = usage or ModelUsage()
    return ModelCallObservation(
        call_kind=ModelCallKind.INTENT,
        elapsed_ms=_elapsed_ms(started, monotonic),
        request_count=request_count,
        input_tokens=safe_usage.input_tokens,
        output_tokens=safe_usage.output_tokens,
        fallback_code=fallback_code,
    )


def _accepted_model_draft(draft: IntentDraft) -> bool:
    if draft.source is not IntentSource.MODEL or any(
        scrub_text(value) != value for value in model_text_values(draft)
    ):
        return False
    try:
        ProviderIntentResponse(
            intent=draft.intent,
            slots=ProviderIntentSlots.model_validate(dict(draft.slots)),
            missing=draft.missing,
            confidence=draft.confidence,
        )
    except ValidationError:
        # Adapter 已经做过同一 wire-schema 校验；这里仍在 application port 边界
        # 重放，避免未来 adapter 或错误实现把越权槽位送进 Resolver/Planner。
        return False
    return True


def _digest(payload: object) -> str:
    return content_digest(canonical_json(payload).decode("utf-8"))


def _opaque_text_digest(value: str) -> str:
    """摘要化被模型输入边界拒绝的文本，包括非法 UTF-8 surrogate。"""
    safe = scrub_text(value)
    return hashlib.sha256(safe.encode("utf-8", errors="surrogatepass")).hexdigest()


def intent_input_digest(
    request: ModelIntentRequest,
    *,
    profile: ModelInvocationProfile,
    origin: Literal["model", "rule"] = "model",
) -> str:
    """模型结果绑定 provider profile；规则结果只绑定其真实输入。"""
    payload: dict[str, object] = {
        "request": dump_contract(request),
        "intent_origin": origin,
    }
    if origin == "model":
        payload["model_identity"] = {
            "provider": profile.provider,
            "model": profile.model,
            "origin": profile.origin,
            "api_version": profile.api_version,
            "prompt_revision": profile.intent_prompt_revision,
            "schema_revision": profile.intent_schema_revision,
        }
    return _digest(payload)


def _rejected_intent_input_digest(
    *,
    envelope: RequestEnvelope,
    history: tuple[str, ...],
    context_truncated: bool,
) -> str:
    """规则降级的拒绝输入事实不依赖任何模型 profile。"""
    payload: dict[str, object] = {
        "rejected_input": True,
        "intent_origin": "rule",
        "user_text_digest": _opaque_text_digest(envelope.text),
        "history_digests": tuple(_opaque_text_digest(item) for item in history),
    }
    if context_truncated:
        payload["context_truncated"] = True
    return _digest(payload)


def intent_result_digest(draft: IntentDraft) -> str:
    return _digest({"draft": dump_contract(draft)})


def intent_artifact_identity_matches(
    artifact: AcceptedIntentArtifact, *, profile: ModelInvocationProfile
) -> bool:
    """复验持久化 identity；不能只相信一个可与元数据分别漂移的 digest。"""
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
            profile.intent_prompt_revision,
            profile.intent_schema_revision,
        )
    return (
        artifact.origin == "rule"
        and artifact.provider is None
        and artifact.model is None
        and artifact.provider_origin is None
    )


async def request_intent_draft(
    *,
    request: ModelIntentRequest,
    model: IntentModelPort | None,
    profile: ModelInvocationProfile,
    fallback: IntentFallback,
    monotonic: MonotonicClock = time.monotonic,
    sleep: AsyncSleep = asyncio.sleep,
) -> IntentStageResult:
    """在固定共享预算内请求 intent；普通失败降级，调用方取消原样传播。"""
    started = monotonic()
    if model is None:
        return IntentStageResult(
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
        async with _stage_timeout(profile.intent_timeout_seconds):
            while request_count < 2:
                request_count += 1
                try:
                    candidate = await model.generate_intent(request)
                except ModelPortError as exc:
                    if (
                        exc.code in INTENT_RETRYABLE_ERROR_CODES
                        and request_count < 2
                    ):
                        # 不引入独立 backoff 配置；让出事件循环后，第二次调用仍受同一
                        # 60 秒 deadline 约束。
                        await sleep(0)
                        continue
                    failure = fallback_code_for_model_error(exc.code)
                    break
                if not _accepted_model_draft(candidate.draft):
                    failure = ModelFallbackCode.INVALID_RESPONSE
                    break
                result = candidate
                break
    except TimeoutError:
        # 只有本函数自己的 asyncio.timeout 会在这里转换为 TimeoutError。调用方取消
        # 仍是 CancelledError，不会被这一分支误记为 provider fallback。
        failure = ModelFallbackCode.APPLICATION_TIMEOUT

    if result is not None:
        return IntentStageResult(
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
    return IntentStageResult(
        draft=fallback(),
        usage=ModelUsage(),
        observation=_observation(
            started=started,
            monotonic=monotonic,
            request_count=request_count,
            fallback_code=failure or ModelFallbackCode.UNAVAILABLE,
        ),
    )


async def load_or_accept_intent(
    *,
    grant: TaskAttemptGrant,
    envelope: RequestEnvelope,
    context: RequestContext,
    history: tuple[str, ...],
    context_truncated: bool = False,
    interpreter: IntentInterpreter,
    model: IntentModelPort | None,
    profile: ModelInvocationProfile,
    artifacts: ModelArtifactStore,
    monotonic: MonotonicClock = time.monotonic,
    sleep: AsyncSleep = asyncio.sleep,
) -> AcceptedIntentResult:
    """重建输入、复用匹配 winner，或调用模型并在 Resolver 前保存 winner。"""
    request: ModelIntentRequest | None
    try:
        built_request = build_model_intent_request(
            user_text=envelope.text,
            history=history,
            context_truncated=context_truncated,
        )
    except ModelInputRejectedError:
        # 合法 RequestEnvelope 的文本可以超过模型单字段上限。该分支仍需要一个可重建
        # digest，但不得把被拒绝的原文写入 artifact。
        request = None
    else:
        request = built_request

    existing = await artifacts.load_intent(task_id=grant.task_id)
    if existing is not None:
        input_digest = (
            _rejected_intent_input_digest(
                envelope=envelope,
                history=history,
                context_truncated=context_truncated,
            )
            if request is None
            else intent_input_digest(
                request,
                profile=profile,
                origin=existing.origin,
            )
        )
        if (
            (request is None and existing.origin != "rule")
            or not intent_artifact_identity_matches(existing, profile=profile)
            or existing.input_digest != input_digest
            or existing.result_digest != intent_result_digest(existing.draft)
        ):
            raise ModelArtifactConflictError(
                "stored accepted intent does not match rebuilt input",
                task_id=grant.task_id,
            )
        return AcceptedIntentResult(artifact=existing, observation=None)

    def fallback() -> IntentDraft:
        return interpreter.interpret(text=envelope.text, context=context)
    if request is None:
        started = monotonic()
        stage = IntentStageResult(
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
        stage = await request_intent_draft(
            request=request,
            model=model,
            profile=profile,
            fallback=fallback,
            monotonic=monotonic,
            sleep=sleep,
        )
    is_model = stage.draft.source is IntentSource.MODEL
    artifact_origin: Literal["model", "rule"] = "model" if is_model else "rule"
    input_digest = (
        _rejected_intent_input_digest(
            envelope=envelope,
            history=history,
            context_truncated=context_truncated,
        )
        if request is None
        else intent_input_digest(
            request,
            profile=profile,
            origin=artifact_origin,
        )
    )
    candidate = IntentArtifactCandidate(
        draft=stage.draft,
        origin=artifact_origin,
        provider=profile.provider if is_model else None,
        model=profile.model if is_model else None,
        provider_origin=profile.origin if is_model else None,
        prompt_revision=(
            profile.intent_prompt_revision
            if is_model
            else RULE_INTENT_PROMPT_REVISION
        ),
        schema_revision=(
            profile.intent_schema_revision
            if is_model
            else RULE_INTENT_SCHEMA_REVISION
        ),
        input_digest=input_digest,
        result_digest=intent_result_digest(stage.draft),
        usage=stage.usage,
    )
    artifact = await artifacts.save_intent(grant=grant, candidate=candidate)
    return AcceptedIntentResult(artifact=artifact, observation=stage.observation)


__all__ = [
    "RULE_INTENT_PROMPT_REVISION",
    "RULE_INTENT_SCHEMA_REVISION",
    "AcceptedIntentResult",
    "IntentStageResult",
    "ModelInputRejectedError",
    "build_model_intent_request",
    "intent_artifact_identity_matches",
    "intent_input_digest",
    "intent_result_digest",
    "load_or_accept_intent",
    "request_intent_draft",
]

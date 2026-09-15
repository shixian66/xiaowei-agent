"""无执行权的模型 interaction 分类预算与确定性降级。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias

from pydantic import ValidationError

from xiaowei_agent.application.model_ports import (
    InteractionClassifierPort,
    ModelPortError,
    fallback_code_for_model_error,
)
from xiaowei_agent.contracts import (
    MAX_MODEL_TEXT_CHARACTERS,
    ClarificationContext,
    IntentSource,
    InteractionClassifierRequest,
    InteractionDraft,
    InteractionSource,
    ModelCallKind,
    ModelCallObservation,
    ModelFallbackCode,
    ModelInvocationProfile,
    ModelUsage,
    ProviderIntentResponse,
    ProviderIntentSlots,
)
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.redaction import scrub_text

InteractionFallback: TypeAlias = Callable[[], InteractionDraft]

_stage_timeout = asyncio.timeout
_SIGNED_BIGINT_MAX: Final[int] = 2**63 - 1


@dataclass(frozen=True)
class InteractionStageResult:
    """最终交给 Router 的候选及安全模型阶段元数据。"""

    draft: InteractionDraft
    usage: ModelUsage
    observation: ModelCallObservation


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


__all__ = [
    "InteractionStageResult",
    "ModelInputRejectedError",
    "build_interaction_classifier_request",
    "request_interaction_draft",
]

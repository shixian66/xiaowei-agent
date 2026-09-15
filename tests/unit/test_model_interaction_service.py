"""Application 级 interaction 分类模型预算与取消语义。"""

import asyncio

import pytest

from xiaowei_agent.application.model_interaction import (
    ModelInputRejectedError,
    build_interaction_classifier_request,
    request_interaction_draft,
)
from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.contracts import (
    ClarificationContext,
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelCallKind,
    ModelErrorCode,
    ModelFallbackCode,
    ModelInvocationProfile,
    ModelUsage,
    RouteSubject,
    RoutingDisposition,
)


class _ScriptedInteractionPort:
    def __init__(self, *results: InteractionModelResult | ModelErrorCode) -> None:
        self.results = list(results)
        self.requests: list[object] = []

    async def classify(self, request: object) -> InteractionModelResult:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, ModelErrorCode):
            raise ModelPortError(result)
        return result


def _capability_draft(*, source: IntentSource = IntentSource.MODEL) -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"window_minutes": "30"},
        missing=(),
        confidence=0.8,
        source=source,
    )


def _model_result(
    *,
    source: InteractionSource = InteractionSource.MODEL,
    capability_source: IntentSource = IntentSource.MODEL,
) -> InteractionModelResult:
    return InteractionModelResult(
        draft=InteractionDraft(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            routing_disposition=RoutingDisposition.PROCEED,
            capability_draft=_capability_draft(source=capability_source),
            confidence=0.8,
            source=source,
        ),
        usage=ModelUsage(input_tokens=12, output_tokens=4),
    )


def _fallback() -> InteractionDraft:
    return InteractionDraft(
        proposed_kind=InteractionKind.UNKNOWN,
        routing_disposition=RoutingDisposition.CLARIFY,
        capability_draft=None,
        confidence=0.0,
        source=InteractionSource.RULE,
    )


def test_request_builder_scrubs_current_text_and_keeps_typed_parent_context() -> None:
    parent = ClarificationContext(
        subject=RouteSubject(kind="route", proposed_kind=InteractionKind.UNKNOWN),
        confirmed_slots=(),
    )
    secret = "token=" + "fake-value"

    request = build_interaction_classifier_request(
        user_text="继续处理 " + secret,
        clarification=parent,
    )

    assert request.clarification == parent
    assert secret not in request.model_dump_json()


@pytest.mark.parametrize("text", ["x" * 8_193, "bad-surrogate-\ud800"])
def test_request_builder_rejects_invalid_current_text(text: str) -> None:
    with pytest.raises(ModelInputRejectedError):
        build_interaction_classifier_request(user_text=text)


@pytest.mark.asyncio
async def test_missing_model_uses_rule_fallback_without_provider_request() -> None:
    result = await request_interaction_draft(
        request=build_interaction_classifier_request(user_text="检查慢查询"),
        model=None,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == _fallback()
    assert result.observation.call_kind is ModelCallKind.INTERACTION
    assert result.observation.request_count == 0
    assert result.observation.fallback_code is ModelFallbackCode.DISABLED


@pytest.mark.asyncio
async def test_retryable_provider_error_falls_back_without_second_request() -> None:
    port = _ScriptedInteractionPort(ModelErrorCode.RATE_LIMITED)

    result = await request_interaction_draft(
        request=build_interaction_classifier_request(user_text="检查慢查询"),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == _fallback()
    assert len(port.requests) == 1
    assert result.observation.request_count == 1
    assert result.observation.fallback_code is ModelFallbackCode.RATE_LIMITED


@pytest.mark.asyncio
async def test_valid_model_candidate_is_returned_after_application_revalidation() -> None:
    model_result = _model_result()
    port = _ScriptedInteractionPort(model_result)

    result = await request_interaction_draft(
        request=build_interaction_classifier_request(user_text="检查慢查询"),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == model_result.draft
    assert result.usage == model_result.usage
    assert result.observation.request_count == 1
    assert result.observation.fallback_code is None


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_without_running_fallback() -> None:
    entered = asyncio.Event()
    fallback_calls = 0

    class BlockingPort:
        async def classify(self, request: object) -> InteractionModelResult:
            del request
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    def fallback() -> InteractionDraft:
        nonlocal fallback_calls
        fallback_calls += 1
        return _fallback()

    task = asyncio.create_task(
        request_interaction_draft(
            request=build_interaction_classifier_request(user_text="检查慢查询"),
            model=BlockingPort(),
            profile=ModelInvocationProfile(),
            fallback=fallback,
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert fallback_calls == 0


@pytest.mark.asyncio
async def test_spoofed_non_model_source_is_rejected_as_a_whole() -> None:
    port = _ScriptedInteractionPort(_model_result(source=InteractionSource.RULE))

    result = await request_interaction_draft(
        request=build_interaction_classifier_request(user_text="检查慢查询"),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == _fallback()
    assert result.observation.fallback_code is ModelFallbackCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_nested_capability_draft_is_revalidated_at_application_boundary() -> None:
    port = _ScriptedInteractionPort(
        _model_result(capability_source=IntentSource.USER)
    )

    result = await request_interaction_draft(
        request=build_interaction_classifier_request(user_text="检查慢查询"),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == _fallback()
    assert result.observation.fallback_code is ModelFallbackCode.INVALID_RESPONSE

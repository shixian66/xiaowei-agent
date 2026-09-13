"""Application 级 intent 模型预算与取消语义。"""

import asyncio
from typing import Any

import pytest

from xiaowei_agent.application import model_intent as model_intent_module
from xiaowei_agent.application.model_intent import (
    ModelInputRejectedError,
    build_model_intent_request,
    request_intent_draft,
)
from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.contracts import (
    IntentDraft,
    IntentModelResult,
    IntentSource,
    ModelErrorCode,
    ModelFallbackCode,
    ModelIntentRequest,
    ModelInvocationProfile,
    ModelUsage,
)
from xiaowei_agent.interfaces.gemini_model import GeminiModelAdapter


class _BlockingModels:
    async def generate_content(self, **kwargs: Any) -> object:
        del kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ClosingAsyncClient:
    def __init__(self) -> None:
        self.models = _BlockingModels()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        await asyncio.sleep(0)
        self.close_finished.set()


class _BlockingClient:
    def __init__(self) -> None:
        self.aio = _ClosingAsyncClient()


class _ScriptedIntentPort:
    def __init__(self, *results: IntentModelResult | ModelErrorCode) -> None:
        self.results = list(results)
        self.requests: list[ModelIntentRequest] = []

    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, ModelErrorCode):
            raise ModelPortError(result)
        return result


def _model_result(*, source: IntentSource = IntentSource.MODEL) -> IntentModelResult:
    return IntentModelResult(
        draft=IntentDraft(
            intent="starrocks.slow_query.diagnose",
            slots={"window_minutes": "30"},
            missing=(),
            confidence=0.8,
            source=source,
        ),
        usage=ModelUsage(input_tokens=12, output_tokens=4),
    )


def _fallback() -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"environment_id": "dev"},
        missing=(),
        confidence=0.9,
        source=IntentSource.USER,
    )


@pytest.mark.asyncio
async def test_outer_timeout_waits_for_adapter_close_and_does_not_run_fallback() -> None:
    """调用方取消不是 provider fallback；adapter 清理完成后再传播。"""
    client = _BlockingClient()
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        secret_reader=lambda path: "AIza" + "x" * 35,
    )
    fallback_calls = 0

    def fallback() -> IntentDraft:
        nonlocal fallback_calls
        fallback_calls += 1
        return IntentDraft(
            intent="unknown",
            slots={},
            missing=(),
            confidence=0.0,
            source=IntentSource.USER,
        )

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await request_intent_draft(
                request=ModelIntentRequest(
                    user_text="检查慢查询",
                    history=(),
                    context_truncated=False,
                ),
                model=adapter,
                profile=ModelInvocationProfile(),
                fallback=fallback,
            )

    assert client.aio.close_started.is_set()
    assert client.aio.close_finished.is_set()
    assert fallback_calls == 0


def test_request_builder_scrubs_and_drops_oldest_complete_history() -> None:
    secret = "token=" + "fake-value"
    request = build_model_intent_request(
        user_text="检查慢查询 " + secret,
        history=tuple(str(index) * 4_000 for index in range(21)),
    )

    assert request.context_truncated is True
    assert len(request.history) <= 20
    assert secret not in request.model_dump_json()
    assert request.history[-1] == "20" * 4_000


def test_request_builder_preserves_upstream_complete_round_truncation() -> None:
    request = build_model_intent_request(
        user_text="检查慢查询",
        history=("retained parent round",),
        context_truncated=True,
    )

    assert request.history == ("retained parent round",)
    assert request.context_truncated is True


@pytest.mark.parametrize(
    "text",
    ["x" * 8_193, "bad-surrogate-\ud800"],
    ids=["one-character-over", "invalid-utf8"],
)
def test_request_builder_rejects_invalid_current_text(text: str) -> None:
    with pytest.raises(ModelInputRejectedError):
        build_model_intent_request(user_text=text, history=())


def test_request_builder_accepts_the_exact_four_byte_character_ceiling() -> None:
    text = "😀" * 8_192

    request = build_model_intent_request(user_text=text, history=())

    assert request.user_text == text
    assert len(request.user_text.encode("utf-8")) == 32 * 1_024


def test_history_overflow_drops_a_complete_oldest_round() -> None:
    history = (*tuple("😀" * 3_200 for _ in range(20)), "x")

    request = build_model_intent_request(user_text="检查慢查询", history=history)

    assert request.context_truncated is True
    assert request.history == history[-20:]


def test_discarded_history_is_not_validated_or_processed_as_model_input() -> None:
    history = ("x" * 8_193, *tuple(f"safe-{index}" for index in range(20)))

    request = build_model_intent_request(user_text="检查慢查询", history=history)

    assert request.context_truncated is True
    assert request.history == history[-20:]


@pytest.mark.asyncio
async def test_retryable_error_gets_one_retry_inside_the_shared_budget() -> None:
    port = _ScriptedIntentPort(ModelErrorCode.RATE_LIMITED, _model_result())

    result = await request_intent_draft(
        request=build_model_intent_request(user_text="检查慢查询", history=()),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft.source is IntentSource.MODEL
    assert result.observation.request_count == 2
    assert result.observation.fallback_code is None
    assert len(port.requests) == 2


@pytest.mark.asyncio
async def test_non_retryable_error_falls_back_once() -> None:
    port = _ScriptedIntentPort(ModelErrorCode.UNAUTHORIZED)

    result = await request_intent_draft(
        request=build_model_intent_request(user_text="检查慢查询", history=()),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft.source is IntentSource.USER
    assert result.observation.request_count == 1
    assert result.observation.fallback_code.value == "model_unauthorized"


@pytest.mark.asyncio
async def test_application_timeout_falls_back_once_without_a_third_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingPort:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_intent(
            self, request: ModelIntentRequest
        ) -> IntentModelResult:
            del request
            self.calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    port = BlockingPort()
    monkeypatch.setattr(
        model_intent_module,
        "_stage_timeout",
        lambda _: asyncio.timeout(0.01),
    )

    result = await request_intent_draft(
        request=build_model_intent_request(user_text="检查慢查询", history=()),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert port.calls == 1
    assert result.draft == _fallback()
    assert result.observation.fallback_code.value == "model_application_timeout"


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_without_running_fallback() -> None:
    entered = asyncio.Event()
    fallback_calls = 0

    class BlockingPort:
        async def generate_intent(
            self, request: ModelIntentRequest
        ) -> IntentModelResult:
            del request
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    def fallback() -> IntentDraft:
        nonlocal fallback_calls
        fallback_calls += 1
        return _fallback()

    task = asyncio.create_task(
        request_intent_draft(
            request=build_model_intent_request(user_text="检查慢查询", history=()),
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
    port = _ScriptedIntentPort(_model_result(source=IntentSource.USER))

    result = await request_intent_draft(
        request=build_model_intent_request(user_text="检查慢查询", history=()),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft.source is IntentSource.USER
    assert result.observation.fallback_code.value == "model_invalid_response"


@pytest.mark.asyncio
async def test_output_that_would_change_under_redaction_is_rejected() -> None:
    unsafe = IntentModelResult(
        draft=IntentDraft(
            intent="starrocks.slow_query.diagnose",
            slots={"window_minutes": "token=" + "fixture-value"},
            missing=(),
            confidence=0.8,
            source=IntentSource.MODEL,
        ),
        usage=ModelUsage(input_tokens=10, output_tokens=3),
    )
    port = _ScriptedIntentPort(unsafe)

    result = await request_intent_draft(
        request=build_model_intent_request(user_text="检查慢查询", history=()),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == _fallback()
    assert result.observation.fallback_code is ModelFallbackCode.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "slots", "missing"),
    [
        (
            "starrocks.slow_query.diagnose",
            {"sql": "SELECT synthetic"},
            (),
        ),
        ("unknown-provider-intent", {}, ()),
        (
            "starrocks.slow_query.diagnose",
            {},
            ("approval_ref",),
        ),
    ],
    ids=["authority-slot", "unknown-intent", "authority-missing"],
)
async def test_application_revalidates_provider_intent_allowlists(
    intent: str,
    slots: dict[str, str],
    missing: tuple[str, ...],
) -> None:
    port = _ScriptedIntentPort(
        IntentModelResult(
            draft=IntentDraft(
                intent=intent,
                slots=slots,
                missing=missing,
                confidence=0.8,
                source=IntentSource.MODEL,
            ),
            usage=ModelUsage(input_tokens=10, output_tokens=3),
        )
    )

    result = await request_intent_draft(
        request=build_model_intent_request(user_text="检查慢查询", history=()),
        model=port,
        profile=ModelInvocationProfile(),
        fallback=_fallback,
    )

    assert result.draft == _fallback()
    assert result.observation.fallback_code is ModelFallbackCode.INVALID_RESPONSE

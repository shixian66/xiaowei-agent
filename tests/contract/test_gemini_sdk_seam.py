"""Gemini SDK 只经一个异步、固定 profile 的 adapter seam 使用。"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import errors, types
from google.genai import models as genai_models
from tests.fakes.model import assert_safe_model_port_error

from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentDraft,
    IntentModelResult,
    IntentSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelIntentRequest,
    ModelInvocationProfile,
    ModelUsage,
    ProviderIntentResponse,
    SlowQueryAdvisoryRequest,
)
from xiaowei_agent.interfaces import gemini_model
from xiaowei_agent.interfaces.gemini_model import (
    GEMINI_API_VERSION,
    GEMINI_MODEL,
    GEMINI_PROVIDER_ORIGIN,
    GeminiModelAdapter,
)


class RecordingModels:
    def __init__(self, response_text: object, usage_metadata: object = None) -> None:
        self.response_text = response_text
        self.usage_metadata = usage_metadata
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            text=self.response_text,
            usage_metadata=self.usage_metadata,
        )


class RecordingAsyncClient:
    def __init__(self, models: RecordingModels) -> None:
        self.models = models
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class RecordingClient:
    def __init__(self, response_text: object, usage_metadata: object = None) -> None:
        self.aio = RecordingAsyncClient(RecordingModels(response_text, usage_metadata))


class RecordingClientFactory:
    def __init__(self, response_text: object, usage_metadata: object = None) -> None:
        self.response_text = response_text
        self.usage_metadata = usage_metadata
        self.calls: list[dict[str, Any]] = []
        self.clients: list[RecordingClient] = []

    def __call__(self, **kwargs: Any) -> RecordingClient:
        self.calls.append(kwargs)
        client = RecordingClient(self.response_text, self.usage_metadata)
        self.clients.append(client)
        return client


def _intent_json(**updates: object) -> str:
    payload: dict[str, object] = {
        "intent": "starrocks.slow_query.diagnose",
        "slots": {"window_minutes": "30"},
        "missing": [],
        "confidence": 0.8,
    }
    payload.update(updates)
    return json.dumps(payload)


def _sdk_response(text: str) -> dict[str, object]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
    }


@pytest.mark.parametrize("call_kind", ["intent", "advisory"])
@pytest.mark.asyncio
async def test_locked_sdk_outer_async_path_transforms_schema_once_without_afc_or_network(
    call_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    response_text = (
        _intent_json()
        if call_kind == "intent"
        else json.dumps(
            {"analysis": "分析", "suggestions": [], "uncertainties": []}
        )
    )

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_sdk_response(response_text))

    transport = httpx.MockTransport(handle)

    def real_client_factory(**kwargs: Any) -> genai.Client:
        options = kwargs["http_options"].model_copy(
            update={
                "async_client_args": {
                    "transport": transport,
                    "trust_env": False,
                }
            }
        )
        return genai.Client(**{**kwargs, "http_options": options})

    for name in gemini_model._PROXY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(genai_models.AsyncModels, "_logged_afc_warning", False)
    adapter = GeminiModelAdapter(
        client_factory=real_client_factory,
        api_key="AIza" + "r" * 35,
    )

    with caplog.at_level("WARNING", logger="google.genai.models"):
        if call_kind == "intent":
            result = await adapter.generate_intent(
                ModelIntentRequest(
                    user_text="x", history=(), context_truncated=False
                )
            )
            assert isinstance(result, IntentModelResult)
        else:
            result = await adapter.generate_advisory(
                SlowQueryAdvisoryRequest(
                    rows=({"queryId": "q"},), sampled=False
                ),
                max_output_tokens=100,
            )
            assert isinstance(result, AdvisoryModelResult)

    assert len(requests) == 1
    assert result.usage == ModelUsage(input_tokens=3, output_tokens=2)
    assert "automatic function calling" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_intent_uses_fixed_developer_api_structured_async_call() -> None:
    factory = RecordingClientFactory(_intent_json())
    adapter = GeminiModelAdapter(
        client_factory=factory,
        api_key="AIza" + "x" * 35,
    )

    result = await adapter.generate_intent(
        ModelIntentRequest(
            user_text="检查慢查询",
            history=(),
            context_truncated=False,
        )
    )

    assert result == IntentModelResult(
        draft=IntentDraft(
            intent="starrocks.slow_query.diagnose",
            slots={"window_minutes": "30"},
            missing=(),
            confidence=0.8,
            source=IntentSource.MODEL,
        ),
        usage=ModelUsage(input_tokens=None, output_tokens=None),
    )
    assert GEMINI_MODEL == "gemini-3-flash-preview"
    assert GEMINI_API_VERSION == "v1beta"
    assert GEMINI_PROVIDER_ORIGIN == "https://generativelanguage.googleapis.com"
    assert len(factory.calls) == 1
    constructor = factory.calls[0]
    assert constructor["vertexai"] is False
    assert constructor["api_key"] == "AIza" + "x" * 35
    options = constructor["http_options"]
    assert options.base_url == GEMINI_PROVIDER_ORIGIN
    assert options.api_version == GEMINI_API_VERSION
    assert options.retry_options is None
    call = factory.clients[0].aio.models.calls[0]
    assert call["model"] == GEMINI_MODEL
    assert call["contents"] == '{"user_text":"检查慢查询","history":[],"context_truncated":false}'
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is ProviderIntentResponse
    assert config.max_output_tokens == 2_048
    assert config.thinking_config.thinking_level.value == "LOW"
    assert config.automatic_function_calling.disable is True
    assert config.tools is None
    assert factory.clients[0].aio.close_calls == 1


@pytest.mark.asyncio
async def test_intent_rejects_explicit_null_slot_from_provider_json() -> None:
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(
            _intent_json(slots={"window_minutes": None})
        ),
        api_key="AIza" + "x" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)


@pytest.mark.asyncio
async def test_advisory_uses_high_thinking_and_plan_bounded_output() -> None:
    response = json.dumps(
        {"analysis": "存在扫描放大", "suggestions": ["检查分区裁剪"], "uncertainties": []}
    )
    factory = RecordingClientFactory(response)
    adapter = GeminiModelAdapter(
        client_factory=factory,
        api_key="AIza" + "y" * 35,
    )
    request = SlowQueryAdvisoryRequest(
        rows=({"queryId": "q-1", "scanRows": 10},),
        sampled=False,
    )

    result = await adapter.generate_advisory(request, max_output_tokens=1_234)

    assert result == AdvisoryModelResult(
        advisory=ModelAdvisory(
            analysis="存在扫描放大",
            suggestions=("检查分区裁剪",),
            uncertainties=(),
        ),
        usage=ModelUsage(input_tokens=None, output_tokens=None),
    )
    call = factory.clients[0].aio.models.calls[0]
    assert call["contents"] == (
        '{"rows":[{"queryId":"q-1","scanRows":10}],"sampled":false}'
    )
    config = call["config"]
    assert config.response_schema is ModelAdvisory
    assert config.max_output_tokens == 1_234
    assert config.thinking_config.thinking_level.value == "HIGH"
    assert config.automatic_function_calling.disable is True
    assert factory.clients[0].aio.close_calls == 1


@pytest.mark.parametrize("value", [True, 0, -1, 4_001])
@pytest.mark.asyncio
async def test_advisory_rejects_invalid_output_budget_before_client_construction(
    value: object,
) -> None:
    factory = RecordingClientFactory("{}")
    adapter = GeminiModelAdapter(
        client_factory=factory,
        api_key="AIza" + "z" * 35,
    )
    request = SlowQueryAdvisoryRequest(
        rows=({"queryId": "q"},),
        sampled=False,
    )

    with pytest.raises(ValueError, match="max_output_tokens"):
        await adapter.generate_advisory(request, max_output_tokens=value)  # type: ignore[arg-type]
    assert factory.calls == []


@pytest.mark.asyncio
async def test_client_is_closed_when_generation_raises() -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            self.calls.append(kwargs)
            raise TimeoutError("private provider detail")

    factory = RecordingClientFactory("{}")
    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")

    def build(**kwargs: Any) -> RecordingClient:
        factory.calls.append(kwargs)
        return client

    adapter = GeminiModelAdapter(
        client_factory=build,
        api_key="AIza" + "w" * 35,
    )
    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )
    assert_safe_model_port_error(caught.value, ModelErrorCode.TIMEOUT)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_valid_usage_metadata_is_returned_with_the_typed_result() -> None:
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=123,
        candidates_token_count=45,
    )
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(_intent_json(), metadata),
        api_key="AIza" + "u" * 35,
    )

    result = await adapter.generate_intent(
        ModelIntentRequest(user_text="x", history=(), context_truncated=False)
    )

    assert result.usage == ModelUsage(input_tokens=123, output_tokens=45)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            types.GenerateContentResponseUsageMetadata(
                candidates_token_count=1
            ),
            ModelUsage(input_tokens=None, output_tokens=1),
        ),
        (
            types.GenerateContentResponseUsageMetadata(prompt_token_count=1),
            ModelUsage(input_tokens=1, output_tokens=None),
        ),
    ],
)
@pytest.mark.parametrize("call_kind", ["intent", "advisory"])
@pytest.mark.asyncio
async def test_usage_metadata_accepts_each_independently_missing_count(
    metadata: types.GenerateContentResponseUsageMetadata,
    expected: ModelUsage,
    call_kind: str,
) -> None:
    response = (
        _intent_json()
        if call_kind == "intent"
        else json.dumps(
            {"analysis": "分析", "suggestions": [], "uncertainties": []}
        )
    )
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(response, metadata),
        api_key="AIza" + "u" * 35,
    )

    if call_kind == "intent":
        result = await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )
    else:
        result = await adapter.generate_advisory(
            SlowQueryAdvisoryRequest(rows=({"queryId": "q"},), sampled=False),
            max_output_tokens=100,
        )

    assert result.usage == expected


def test_adapter_rejects_an_object_mutated_profile() -> None:
    profile = ModelInvocationProfile()
    object.__setattr__(profile, "origin", "https://redirect.invalid")

    with pytest.raises(ValueError, match="fixed RI3 profile"):
        GeminiModelAdapter(profile=profile, api_key="AIza" + "u" * 35)


@pytest.mark.parametrize("value", [-1, 2**63])
@pytest.mark.asyncio
async def test_usage_metadata_rejects_negative_and_overflow_after_sdk_normalization(
    value: int,
) -> None:
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=value,
        candidates_token_count=1,
    )
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(_intent_json(), metadata),
        api_key="AIza" + "u" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)


@pytest.mark.asyncio
async def test_usage_metadata_accepts_explicit_nullable_fields() -> None:
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=None,
        candidates_token_count=None,
    )
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(_intent_json(), metadata),
        api_key="AIza" + "u" * 35,
    )

    result = await adapter.generate_intent(
        ModelIntentRequest(user_text="x", history=(), context_truncated=False)
    )

    assert result.usage == ModelUsage(input_tokens=None, output_tokens=None)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (False, 0),
        (True, 1),
        (0.0, 0),
        (1.0, 1),
        ("1", 1),
        ("1.0", 1),
    ],
)
def test_locked_sdk_normalizes_integer_like_raw_usage_to_int(
    raw_value: object,
    expected: int,
) -> None:
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=raw_value,
        candidates_token_count=raw_value,
    )

    assert metadata.prompt_token_count == expected
    assert type(metadata.prompt_token_count) is int
    assert metadata.candidates_token_count == expected
    assert type(metadata.candidates_token_count) is int
    assert metadata.model_fields_set == {
        "prompt_token_count",
        "candidates_token_count",
    }


@pytest.mark.asyncio
async def test_legitimate_slow_close_is_not_tuned_to_the_test_wall_clock() -> None:
    class DelayedCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            await asyncio.sleep(0.06)

    client = RecordingClient(_intent_json())
    client.aio = DelayedCloseAsyncClient(RecordingModels(_intent_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "d" * 35,
    )

    result = await adapter.generate_intent(
        ModelIntentRequest(user_text="x", history=(), context_truncated=False)
    )

    assert result.draft.intent == "starrocks.slow_query.diagnose"
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_hung_close_is_bounded_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_model, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    close_started = asyncio.Event()

    class HungAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            close_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                close_finished.set()

    close_finished = asyncio.Event()
    client = RecordingClient(_intent_json())
    client.aio = HungAsyncClient(RecordingModels(_intent_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "h" * 35,
    )

    started = time.monotonic()
    with pytest.raises(ModelPortError) as caught:
        await asyncio.wait_for(
            adapter.generate_intent(
                ModelIntentRequest(user_text="x", history=(), context_truncated=False)
            ),
            timeout=0.3,
        )

    assert close_started.is_set()
    assert close_finished.is_set()
    assert_safe_model_port_error(caught.value, ModelErrorCode.TIMEOUT)
    assert time.monotonic() - started < 0.3


@pytest.mark.asyncio
async def test_close_failure_after_success_is_safely_normalized() -> None:
    class FailingCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("private-close-detail")

    client = RecordingClient(_intent_json())
    client.aio = FailingCloseAsyncClient(RecordingModels(_intent_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "e" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.UNAVAILABLE)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_close_error_does_not_mask_provider_error() -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise TimeoutError("private-provider-detail")

    class FailingCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("private-close-detail")

    client = RecordingClient("{}")
    client.aio = FailingCloseAsyncClient(FailingModels("{}"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "e" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.TIMEOUT)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_close_error_does_not_mask_schema_error() -> None:
    class FailingCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("private-close-detail")

    client = RecordingClient("not-json")
    client.aio = FailingCloseAsyncClient(RecordingModels("not-json"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "s" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_after_bounded_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_model, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    generation_started = asyncio.Event()

    class WaitingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            generation_started.set()
            await asyncio.Event().wait()

    class SlowCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            await asyncio.sleep(0.5)

    client = RecordingClient("{}")
    client.aio = SlowCloseAsyncClient(WaitingModels("{}"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "c" * 35,
    )
    task = asyncio.create_task(
        adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )
    )
    await generation_started.wait()

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.aio.close_calls == 1
    assert time.monotonic() - started < 0.3


@pytest.mark.asyncio
async def test_repeated_outer_cancellation_waits_for_close_completion() -> None:
    generation_started = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_completed = asyncio.Event()

    class WaitingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            generation_started.set()
            await asyncio.Event().wait()

    class ObservableCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            close_started.set()
            await allow_close.wait()
            close_completed.set()

    client = RecordingClient("{}")
    client.aio = ObservableCloseAsyncClient(WaitingModels("{}"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "c" * 35,
    )
    task = asyncio.create_task(
        adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )
    )
    await generation_started.wait()

    task.cancel()
    await close_started.wait()
    task.cancel()
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.3)

    assert close_completed.is_set()
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_close_cancelled_error_does_not_mask_provider_error() -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise TimeoutError("private-provider-detail")

    class CancelledCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise asyncio.CancelledError

    client = RecordingClient("{}")
    client.aio = CancelledCloseAsyncClient(FailingModels("{}"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "c" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.TIMEOUT)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_close_cancelled_error_after_success_is_a_safe_close_failure() -> None:
    class CancelledCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise asyncio.CancelledError

    client = RecordingClient(_intent_json())
    client.aio = CancelledCloseAsyncClient(RecordingModels(_intent_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "c" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.UNAVAILABLE)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_close_base_exception_after_success_is_safely_normalized() -> None:
    class UnsafeCloseError(BaseException):
        pass

    class FailingCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise UnsafeCloseError("private-close-boundary-detail")

    client = RecordingClient(_intent_json())
    client.aio = FailingCloseAsyncClient(RecordingModels(_intent_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "b" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.UNAVAILABLE)
    assert "private-close-boundary-detail" not in str(caught.value)
    assert client.aio.close_calls == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ModelErrorCode.UNAUTHORIZED),
        (403, ModelErrorCode.FORBIDDEN),
        (429, ModelErrorCode.RATE_LIMITED),
        (500, ModelErrorCode.SERVER_ERROR),
        (418, ModelErrorCode.UNAVAILABLE),
    ],
)
@pytest.mark.asyncio
async def test_sdk_api_errors_map_to_closed_codes_without_retained_context(
    status: int, expected: ModelErrorCode
) -> None:
    provider_detail = "private-provider-body"

    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise errors.APIError(status, {"message": provider_detail})

    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "a" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, expected)
    assert provider_detail not in str(caught.value)


@pytest.mark.asyncio
async def test_sdk_api_error_with_non_integer_code_fails_safe() -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise errors.APIError("500", {"message": "private-provider-body"})  # type: ignore[arg-type]

    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "a" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.UNAVAILABLE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        OSError("private-os-detail"),
        httpx.ConnectError("private-httpx-detail"),
    ],
)
async def test_transport_error_maps_to_retryable_transport_code_without_retained_context(
    transport_error: Exception,
) -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise transport_error

    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "o" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.TRANSPORT_ERROR)


@pytest.mark.asyncio
async def test_poisoned_local_model_error_is_replaced_with_a_fresh_safe_error() -> None:
    original = RuntimeError("private-provider-detail")
    poisoned = ModelPortError(ModelErrorCode.RATE_LIMITED)
    try:
        raise poisoned from original
    except ModelPortError as caught:
        poisoned = caught

    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise poisoned

    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        api_key="AIza" + "p" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value is not poisoned
    assert_safe_model_port_error(caught.value, ModelErrorCode.RATE_LIMITED)


@pytest.mark.asyncio
async def test_non_string_response_text_is_invalid_response() -> None:
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory({"not": "text"}),
        api_key="AIza" + "n" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)


@pytest.mark.asyncio
async def test_client_construction_failure_is_safely_normalized() -> None:
    def fail_client(**kwargs: Any) -> object:
        raise OSError("private-client-construction-detail")

    adapter = GeminiModelAdapter(
        client_factory=fail_client,
        api_key="AIza" + "f" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.UNAVAILABLE)

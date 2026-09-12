"""Gemini SDK 只经一个异步、固定 profile 的 adapter seam 使用。"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors

from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentDraft,
    IntentModelResult,
    IntentSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelIntentRequest,
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
    GeminiModelError,
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


@pytest.mark.asyncio
async def test_intent_uses_fixed_developer_api_structured_async_call() -> None:
    factory = RecordingClientFactory(_intent_json())
    adapter = GeminiModelAdapter(
        client_factory=factory,
        secret_reader=lambda path: "AIza" + "x" * 35,
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
    assert config.tools is None
    assert factory.clients[0].aio.close_calls == 1


@pytest.mark.asyncio
async def test_advisory_uses_high_thinking_and_plan_bounded_output() -> None:
    response = json.dumps(
        {"analysis": "存在扫描放大", "suggestions": ["检查分区裁剪"], "uncertainties": []}
    )
    factory = RecordingClientFactory(response)
    adapter = GeminiModelAdapter(
        client_factory=factory,
        secret_reader=lambda path: "AIza" + "y" * 35,
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
    assert factory.clients[0].aio.close_calls == 1


@pytest.mark.parametrize("value", [True, 0, -1, 4_001])
@pytest.mark.asyncio
async def test_advisory_rejects_invalid_output_budget_before_client_construction(
    value: object,
) -> None:
    factory = RecordingClientFactory("{}")
    adapter = GeminiModelAdapter(
        client_factory=factory,
        secret_reader=lambda path: "AIza" + "z" * 35,
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
        secret_reader=lambda path: "AIza" + "w" * 35,
    )
    with pytest.raises(Exception, match="model_timeout") as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )
    assert "private provider detail" not in str(caught.value)
    assert caught.value.__context__ is None
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_valid_usage_metadata_is_returned_with_the_typed_result() -> None:
    metadata = SimpleNamespace(prompt_token_count=123, candidates_token_count=45)
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(_intent_json(), metadata),
        secret_reader=lambda path: "AIza" + "u" * 35,
    )

    result = await adapter.generate_intent(
        ModelIntentRequest(user_text="x", history=(), context_truncated=False)
    )

    assert result.usage == ModelUsage(input_tokens=123, output_tokens=45)


@pytest.mark.parametrize(
    "metadata",
    [
        SimpleNamespace(candidates_token_count=1),
        SimpleNamespace(prompt_token_count=1),
        SimpleNamespace(prompt_token_count=True, candidates_token_count=1),
        SimpleNamespace(prompt_token_count=1.0, candidates_token_count=1),
        SimpleNamespace(prompt_token_count=-1, candidates_token_count=1),
        SimpleNamespace(prompt_token_count=2**63, candidates_token_count=1),
        SimpleNamespace(prompt_token_count=1, candidates_token_count=True),
        SimpleNamespace(prompt_token_count=1, candidates_token_count=1.0),
        SimpleNamespace(prompt_token_count=1, candidates_token_count=-1),
        SimpleNamespace(prompt_token_count=1, candidates_token_count=2**63),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_incomplete_usage_metadata_rejects_the_response(
    metadata: object,
) -> None:
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory(_intent_json(), metadata),
        secret_reader=lambda path: "AIza" + "u" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is ModelErrorCode.INVALID_RESPONSE
    assert caught.value.__context__ is None


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
        secret_reader=lambda path: "AIza" + "d" * 35,
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
            await asyncio.Event().wait()

    client = RecordingClient(_intent_json())
    client.aio = HungAsyncClient(RecordingModels(_intent_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        secret_reader=lambda path: "AIza" + "h" * 35,
    )

    started = time.monotonic()
    with pytest.raises(GeminiModelError) as caught:
        await asyncio.wait_for(
            adapter.generate_intent(
                ModelIntentRequest(user_text="x", history=(), context_truncated=False)
            ),
            timeout=0.3,
        )

    assert close_started.is_set()
    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert caught.value.__context__ is None
    assert time.monotonic() - started < 0.3


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
        secret_reader=lambda path: "AIza" + "e" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert str(caught.value) == "model_timeout"
    assert caught.value.__context__ is None
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
        secret_reader=lambda path: "AIza" + "s" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is ModelErrorCode.INVALID_RESPONSE
    assert str(caught.value) == "model_invalid_response"
    assert caught.value.__context__ is None
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
        secret_reader=lambda path: "AIza" + "c" * 35,
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ModelErrorCode.UNAUTHORIZED),
        (403, ModelErrorCode.FORBIDDEN),
        (429, ModelErrorCode.RATE_LIMITED),
        (500, ModelErrorCode.UNAVAILABLE),
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
        secret_reader=lambda path: "AIza" + "a" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is expected
    assert provider_detail not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_transport_error_maps_to_unavailable_without_retained_context() -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            raise OSError("private-transport-detail")

    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: client,
        secret_reader=lambda path: "AIza" + "o" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is ModelErrorCode.UNAVAILABLE
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_non_string_response_text_is_invalid_response() -> None:
    adapter = GeminiModelAdapter(
        client_factory=RecordingClientFactory({"not": "text"}),
        secret_reader=lambda path: "AIza" + "n" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is ModelErrorCode.INVALID_RESPONSE
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_client_construction_failure_is_safely_normalized() -> None:
    def fail_client(**kwargs: Any) -> object:
        raise OSError("private-client-construction-detail")

    adapter = GeminiModelAdapter(
        client_factory=fail_client,
        secret_reader=lambda path: "AIza" + "f" * 35,
    )

    with pytest.raises(GeminiModelError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value.code is ModelErrorCode.UNAVAILABLE
    assert caught.value.__context__ is None

"""Gemini SDK 只经一个异步、固定 profile 的 adapter seam 使用。"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from xiaowei_agent.contracts import (
    IntentDraft,
    IntentSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelIntentRequest,
    ProviderIntentResponse,
    SlowQueryAdvisoryRequest,
)
from xiaowei_agent.interfaces.gemini_model import (
    GEMINI_API_VERSION,
    GEMINI_MODEL,
    GEMINI_PROVIDER_ORIGIN,
    GeminiModelAdapter,
    GeminiModelError,
)


class RecordingModels:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.response_text, usage_metadata=None)


class RecordingAsyncClient:
    def __init__(self, models: RecordingModels) -> None:
        self.models = models
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class RecordingClient:
    def __init__(self, response_text: str) -> None:
        self.aio = RecordingAsyncClient(RecordingModels(response_text))


class RecordingClientFactory:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []
        self.clients: list[RecordingClient] = []

    def __call__(self, **kwargs: Any) -> RecordingClient:
        self.calls.append(kwargs)
        client = RecordingClient(self.response_text)
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

    assert result == IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"window_minutes": "30"},
        missing=(),
        confidence=0.8,
        source=IntentSource.MODEL,
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

    assert result == ModelAdvisory(
        analysis="存在扫描放大",
        suggestions=("检查分区裁剪",),
        uncertainties=(),
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
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_hung_close_is_bounded_after_success() -> None:
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
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_after_bounded_close() -> None:
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

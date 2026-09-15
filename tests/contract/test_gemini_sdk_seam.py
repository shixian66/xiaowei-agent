"""Gemini SDK 只经一个异步、固定 profile 的 adapter seam 使用。"""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors, types
from tests.fakes.model import assert_safe_model_port_error

from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    InteractionClassifierRequest,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelInvocationProfile,
    ModelUsage,
    ProviderInteractionResponse,
    SlowQueryAdvisoryRequest,
)
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


def _interaction_json(**updates: object) -> str:
    payload: dict[str, object] = {
        "proposed_kind": "capability_request",
        "capability": {
            "intent": "starrocks.slow_query.diagnose",
            "slots": {"window_minutes": "30"},
            "missing": [],
            "confidence": 0.8,
        },
        "confidence": 0.8,
    }
    payload.update(updates)
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_classify_uses_fixed_profile_schema_and_maps_usage() -> None:
    factory = RecordingClientFactory(
        _interaction_json(),
        usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2),
    )
    adapter = GeminiModelAdapter(
        api_key="AIza" + "x" * 35,
        client_factory=factory,
    )

    result = await adapter.classify(InteractionClassifierRequest(user_text="检查慢查询"))

    assert isinstance(result, InteractionModelResult)
    assert result.draft.proposed_kind is InteractionKind.CAPABILITY_REQUEST
    assert result.draft.source is InteractionSource.MODEL
    assert result.draft.capability_draft is not None
    assert result.draft.capability_draft.intent == "starrocks.slow_query.diagnose"
    assert result.usage == ModelUsage(input_tokens=3, output_tokens=2)
    assert factory.calls == [
        {
            "vertexai": False,
            "api_key": "AIza" + "x" * 35,
            "http_options": types.HttpOptions(
                base_url=GEMINI_PROVIDER_ORIGIN,
                api_version=GEMINI_API_VERSION,
                retry_options=None,
            ),
        }
    ]
    call = factory.clients[0].aio.models.calls[0]
    assert call["model"] == GEMINI_MODEL
    assert json.loads(call["contents"]) == {
        "user_text": "检查慢查询",
        "clarification": None,
    }
    assert call["config"].response_schema is ProviderInteractionResponse
    assert call["config"].automatic_function_calling.disable is True
    assert call["config"].max_output_tokens == 2_048
    assert factory.clients[0].aio.close_calls == 1


@pytest.mark.asyncio
async def test_generate_advisory_uses_advisory_schema_and_token_limit() -> None:
    factory = RecordingClientFactory(
        json.dumps({"analysis": "分析", "suggestions": [], "uncertainties": []})
    )
    adapter = GeminiModelAdapter(
        api_key="AIza" + "x" * 35,
        client_factory=factory,
    )

    result = await adapter.generate_advisory(
        SlowQueryAdvisoryRequest(rows=({"queryId": "q1"},), sampled=False),
        max_output_tokens=4_000,
    )

    assert result == AdvisoryModelResult(
        advisory=ModelAdvisory(analysis="分析", suggestions=(), uncertainties=()),
        usage=ModelUsage(),
    )
    call = factory.clients[0].aio.models.calls[0]
    assert call["config"].response_schema is ModelAdvisory
    assert call["config"].max_output_tokens == 4_000


@pytest.mark.asyncio
async def test_classify_rejects_invalid_provider_shape_without_leaking_details() -> None:
    factory = RecordingClientFactory(
        _interaction_json(capability=None),
        usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2),
    )
    adapter = GeminiModelAdapter(
        api_key="AIza" + "x" * 35,
        client_factory=factory,
    )

    with pytest.raises(ModelPortError) as raised:
        await adapter.classify(InteractionClassifierRequest(user_text="检查慢查询"))

    assert_safe_model_port_error(raised.value, ModelErrorCode.INVALID_RESPONSE)
    assert factory.clients[0].aio.close_calls == 1


@pytest.mark.asyncio
async def test_non_string_sdk_text_is_invalid_response() -> None:
    factory = RecordingClientFactory(None)
    adapter = GeminiModelAdapter(
        api_key="AIza" + "x" * 35,
        client_factory=factory,
    )

    with pytest.raises(ModelPortError) as raised:
        await adapter.classify(InteractionClassifierRequest(user_text="x"))

    assert_safe_model_port_error(raised.value, ModelErrorCode.INVALID_RESPONSE)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), ModelErrorCode.TIMEOUT),
        (httpx.ConnectError("boom"), ModelErrorCode.TRANSPORT_ERROR),
        (OSError("boom"), ModelErrorCode.TRANSPORT_ERROR),
        (errors.APIError(401, "unauthorized"), ModelErrorCode.UNAUTHORIZED),
        (errors.APIError(403, "forbidden"), ModelErrorCode.FORBIDDEN),
        (errors.APIError(429, "rate"), ModelErrorCode.RATE_LIMITED),
        (errors.APIError(503, "server"), ModelErrorCode.SERVER_ERROR),
        (RuntimeError("surprise"), ModelErrorCode.UNAVAILABLE),
    ],
)
@pytest.mark.asyncio
async def test_sdk_errors_map_to_closed_model_error_codes(
    error: BaseException, expected: ModelErrorCode
) -> None:
    class RaisingModels:
        async def generate_content(self, **kwargs: Any) -> object:
            del kwargs
            raise error

    class Client:
        def __init__(self) -> None:
            self.aio = SimpleNamespace(models=RaisingModels(), aclose=self.close)
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = Client()
    adapter = GeminiModelAdapter(
        api_key="AIza" + "x" * 35,
        client_factory=lambda **_: client,
    )

    with pytest.raises(ModelPortError) as raised:
        await adapter.classify(InteractionClassifierRequest(user_text="x"))

    assert_safe_model_port_error(raised.value, expected)
    assert client.closed is True


def test_profile_must_be_the_fixed_gemini_profile() -> None:
    profile = ModelInvocationProfile()
    object.__setattr__(profile, "model", "other")

    with pytest.raises(ValueError):
        GeminiModelAdapter(
            api_key="AIza" + "x" * 35,
            profile=profile,
        )

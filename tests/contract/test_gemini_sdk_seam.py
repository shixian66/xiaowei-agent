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


@pytest.mark.parametrize("call_kind", ["interaction", "advisory"])
@pytest.mark.asyncio
async def test_locked_sdk_outer_async_path_transforms_schema_once_without_afc_or_network(
    call_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    response_text = (
        _interaction_json()
        if call_kind == "interaction"
        else json.dumps({"analysis": "分析", "suggestions": [], "uncertainties": []})
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
        if call_kind == "interaction":
            result = await adapter.classify(
                InteractionClassifierRequest(user_text="x")
            )
            assert isinstance(result, InteractionModelResult)
        else:
            result = await adapter.generate_advisory(
                SlowQueryAdvisoryRequest(rows=({"queryId": "q"},), sampled=False),
                max_output_tokens=100,
            )
            assert isinstance(result, AdvisoryModelResult)

    assert len(requests) == 1
    assert result.usage == ModelUsage(input_tokens=3, output_tokens=2)
    assert "automatic function calling" not in caplog.text.lower()


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
    assert call["config"].response_schema is None
    assert (
        call["config"].response_json_schema
        == ProviderInteractionResponse.model_json_schema()
    )
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
    assert call["config"].response_schema is None
    assert call["config"].response_json_schema == ModelAdvisory.model_json_schema()
    assert call["config"].max_output_tokens == 4_000


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            types.GenerateContentResponseUsageMetadata(candidates_token_count=1),
            ModelUsage(input_tokens=None, output_tokens=1),
        ),
        (
            types.GenerateContentResponseUsageMetadata(prompt_token_count=1),
            ModelUsage(input_tokens=1, output_tokens=None),
        ),
    ],
)
@pytest.mark.parametrize("call_kind", ["interaction", "advisory"])
@pytest.mark.asyncio
async def test_usage_metadata_accepts_each_independently_missing_count(
    metadata: types.GenerateContentResponseUsageMetadata,
    expected: ModelUsage,
    call_kind: str,
) -> None:
    response = (
        _interaction_json()
        if call_kind == "interaction"
        else json.dumps({"analysis": "分析", "suggestions": [], "uncertainties": []})
    )
    adapter = GeminiModelAdapter(
        api_key="AIza" + "u" * 35,
        client_factory=RecordingClientFactory(response, metadata),
    )

    if call_kind == "interaction":
        result = await adapter.classify(InteractionClassifierRequest(user_text="x"))
    else:
        result = await adapter.generate_advisory(
            SlowQueryAdvisoryRequest(rows=({"queryId": "q"},), sampled=False),
            max_output_tokens=100,
        )

    assert result.usage == expected


@pytest.mark.parametrize("value", [-1, 2**63])
@pytest.mark.parametrize("field", ["prompt", "candidates"])
@pytest.mark.asyncio
async def test_usage_metadata_rejects_negative_and_overflow_after_sdk_normalization(
    value: int,
    field: str,
) -> None:
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=value if field == "prompt" else 1,
        candidates_token_count=value if field == "candidates" else 1,
    )
    adapter = GeminiModelAdapter(
        api_key="AIza" + "u" * 35,
        client_factory=RecordingClientFactory(_interaction_json(), metadata),
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.classify(InteractionClassifierRequest(user_text="x"))

    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)


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


def test_client_close_timeout_profile_stays_bounded() -> None:
    assert 0 < gemini_model._CLIENT_CLOSE_TIMEOUT_SECONDS <= 1.0


@pytest.mark.asyncio
async def test_hung_close_is_bounded_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_model, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    close_started = asyncio.Event()
    close_finished = asyncio.Event()

    class HungAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            close_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                close_finished.set()

    client = RecordingClient(_interaction_json())
    client.aio = HungAsyncClient(RecordingModels(_interaction_json()))
    adapter = GeminiModelAdapter(
        client_factory=lambda **_: client,
        api_key="AIza" + "h" * 35,
    )

    started = time.monotonic()
    with pytest.raises(ModelPortError) as caught:
        await asyncio.wait_for(
            adapter.classify(InteractionClassifierRequest(user_text="x")),
            timeout=0.3,
        )

    assert close_started.is_set()
    assert close_finished.is_set()
    assert_safe_model_port_error(caught.value, ModelErrorCode.TIMEOUT)
    assert time.monotonic() - started < 0.3


@pytest.mark.asyncio
async def test_close_error_does_not_mask_provider_error() -> None:
    class FailingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            del kwargs
            raise TimeoutError("private-provider-detail")

    class FailingCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("private-close-detail")

    client = RecordingClient("{}")
    client.aio = FailingCloseAsyncClient(FailingModels("{}"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **_: client,
        api_key="AIza" + "e" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.classify(InteractionClassifierRequest(user_text="x"))

    assert_safe_model_port_error(caught.value, ModelErrorCode.TIMEOUT)
    assert client.aio.close_calls == 1


@pytest.mark.asyncio
async def test_outer_cancellation_propagates_after_bounded_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_model, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    generation_started = asyncio.Event()

    class WaitingModels(RecordingModels):
        async def generate_content(self, **kwargs: Any) -> object:
            del kwargs
            generation_started.set()
            await asyncio.Event().wait()

    class SlowCloseAsyncClient(RecordingAsyncClient):
        async def aclose(self) -> None:
            self.close_calls += 1
            await asyncio.sleep(0.5)

    client = RecordingClient("{}")
    client.aio = SlowCloseAsyncClient(WaitingModels("{}"))
    adapter = GeminiModelAdapter(
        client_factory=lambda **_: client,
        api_key="AIza" + "c" * 35,
    )
    task = asyncio.create_task(
        adapter.classify(InteractionClassifierRequest(user_text="x"))
    )
    await generation_started.wait()

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.aio.close_calls == 1
    assert time.monotonic() - started < 0.3


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
            del kwargs
            raise errors.APIError(status, {"message": provider_detail})

    client = RecordingClient("{}")
    client.aio.models = FailingModels("{}")
    adapter = GeminiModelAdapter(
        client_factory=lambda **_: client,
        api_key="AIza" + "a" * 35,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.classify(InteractionClassifierRequest(user_text="x"))

    assert_safe_model_port_error(caught.value, expected)
    assert provider_detail not in str(caught.value)


def test_profile_must_be_the_fixed_gemini_profile() -> None:
    profile = ModelInvocationProfile()
    object.__setattr__(profile, "model", "other")

    with pytest.raises(ValueError):
        GeminiModelAdapter(
            api_key="AIza" + "x" * 35,
            profile=profile,
        )


@pytest.mark.asyncio
async def test_connection_probe_asks_for_a_tiny_answer() -> None:
    """探针只证明“这把 Key 通不通”，不能让模型写满 2048 token 再回来。

    本机实测 gemini-3-flash-preview 对固定探针输入按 2048 上限作答要 6–8 秒，
    超过 Web 探针 5 秒上限，于是“测试连接”永远报超时；上限 16 时约 1 秒。
    """
    factory = RecordingClientFactory("ok")

    await gemini_model.probe_connection(
        api_key="AIza" + "x" * 35, contents="probe", client_factory=factory
    )

    config = factory.clients[0].aio.models.calls[0]["config"]
    assert config.max_output_tokens == gemini_model.GEMINI_PROBE_OUTPUT_TOKENS == 16
    assert config.automatic_function_calling.disable is True


@pytest.mark.parametrize("call_kind", ["interaction", "advisory"])
@pytest.mark.asyncio
async def test_wire_request_sends_the_json_schema_gemini_accepts(
    call_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``response_schema`` 会被 SDK 转成 OpenAPI 子集并带上 ``additional_properties``，
    真实 Gemini 以 400 INVALID_ARGUMENT 拒收（本机实测，任务因此 route_not_available）。
    线上请求必须改走 ``responseJsonSchema``，且原样携带契约自己的 JSON Schema。
    """
    bodies: list[dict[str, Any]] = []
    response_text = (
        _interaction_json()
        if call_kind == "interaction"
        else json.dumps({"analysis": "分析", "suggestions": [], "uncertainties": []})
    )

    async def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_sdk_response(response_text))

    transport = httpx.MockTransport(handle)

    def real_client_factory(**kwargs: Any) -> genai.Client:
        options = kwargs["http_options"].model_copy(
            update={"async_client_args": {"transport": transport, "trust_env": False}}
        )
        return genai.Client(**{**kwargs, "http_options": options})

    for name in gemini_model._PROXY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    adapter = GeminiModelAdapter(
        client_factory=real_client_factory, api_key="AIza" + "w" * 35
    )
    if call_kind == "interaction":
        await adapter.classify(InteractionClassifierRequest(user_text="你好"))
        expected = gemini_model.ProviderInteractionResponse.model_json_schema()
    else:
        await adapter.generate_advisory(
            SlowQueryAdvisoryRequest(rows=({"queryId": "q"},), sampled=False),
            max_output_tokens=100,
        )
        expected = ModelAdvisory.model_json_schema()

    config = bodies[0]["generationConfig"]
    assert "responseSchema" not in config
    assert config["responseJsonSchema"] == expected
    assert config["responseMimeType"] == "application/json"
    assert "additional_properties" not in json.dumps(bodies[0])

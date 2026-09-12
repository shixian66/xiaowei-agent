"""固定 Google Gemini Developer API 的单次请求 SDK adapter。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any, Final, cast

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from xiaowei_agent.application.model_ports import (
    ModelPortError,
    validate_advisory_output_tokens,
)
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
    StrictInt,
)
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.interfaces.secret_file import read_secret_file
from xiaowei_agent.redaction import scrub_text

GEMINI_MODEL_PROFILE: Final[ModelInvocationProfile] = ModelInvocationProfile()
GEMINI_MODEL: Final[str] = GEMINI_MODEL_PROFILE.model
GEMINI_API_VERSION: Final[str] = GEMINI_MODEL_PROFILE.api_version
GEMINI_PROVIDER_ORIGIN: Final[str] = GEMINI_MODEL_PROFILE.origin
GEMINI_SECRET_FILE: Final[str] = "/run/secrets/gemini_api_" + "key"
INTENT_OUTPUT_TOKEN_LIMIT: Final[int] = GEMINI_MODEL_PROFILE.intent_output_tokens
_CLIENT_CLOSE_TIMEOUT_SECONDS: Final[float] = 1.0

_PROXY_ENVIRONMENT: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_SYSTEM_INTENT: Final[str] = (
    "Return only the requested intent JSON. Never choose a capability, target, SQL, "
    "approval, tool, execution step, or next action."
)
_SYSTEM_ADVISORY: Final[str] = (
    "Return only the requested advisory JSON from the supplied untrusted rows. "
    "Do not create facts, evidence references, SQL, commands, links, or next actions."
)


def _validate_credential(value: str) -> str:
    if not 20 <= len(value) <= 256 or value != value.strip() or any(
        character.isspace() or not character.isprintable() for character in value
    ):
        raise ModelPortError(ModelErrorCode.CREDENTIAL_UNAVAILABLE)
    return value


def _require_clean_texts(value: ProviderIntentResponse | ModelAdvisory) -> None:
    if any(scrub_text(text) != text for text in model_text_values(value)):
        raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)


def _map_error(error: BaseException) -> ModelPortError:
    if isinstance(error, ModelPortError):
        return ModelPortError(error.code)
    if isinstance(error, TimeoutError | httpx.TimeoutException):
        return ModelPortError(ModelErrorCode.TIMEOUT)
    if isinstance(error, errors.APIError):
        if error.code == 401:
            return ModelPortError(ModelErrorCode.UNAUTHORIZED)
        if error.code == 403:
            return ModelPortError(ModelErrorCode.FORBIDDEN)
        if error.code == 429:
            return ModelPortError(ModelErrorCode.RATE_LIMITED)
        if 500 <= error.code <= 599:
            return ModelPortError(ModelErrorCode.SERVER_ERROR)
        return ModelPortError(ModelErrorCode.UNAVAILABLE)
    if isinstance(error, httpx.TransportError | OSError):
        return ModelPortError(ModelErrorCode.TRANSPORT_ERROR)
    return ModelPortError(ModelErrorCode.UNAVAILABLE)


def _model_usage(response: types.GenerateContentResponse) -> ModelUsage:
    metadata = response.usage_metadata
    if metadata is None:
        return ModelUsage()
    if not {
        "prompt_token_count",
        "candidates_token_count",
    } <= metadata.model_fields_set:
        raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
    invalid_usage = False
    try:
        usage = ModelUsage(
            input_tokens=metadata.prompt_token_count,
            output_tokens=metadata.candidates_token_count,
        )
    except ValidationError:
        invalid_usage = True
        usage = ModelUsage()
    if invalid_usage:
        raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
    return usage


async def _close_client(
    client: genai.Client, *, primary_error: BaseException | None
) -> None:
    """有界关闭 client；清理失败不遮蔽已在传播的主异常。"""
    mapped: ModelPortError | None = None
    try:
        async with asyncio.timeout(_CLIENT_CLOSE_TIMEOUT_SECONDS):
            await client.aio.aclose()
    except Exception as error:
        if primary_error is None:
            mapped = _map_error(error)
    if mapped is not None:
        raise mapped


class GeminiModelAdapter:
    """只做本地 DTO 与一次 SDK request 的相互转换。"""

    def __init__(
        self,
        *,
        profile: ModelInvocationProfile = GEMINI_MODEL_PROFILE,
        client_factory: Callable[..., Any] = genai.Client,
        secret_reader: Callable[[str], str] = read_secret_file,
    ) -> None:
        if profile != GEMINI_MODEL_PROFILE:
            raise ValueError("Gemini adapter requires the fixed RI3 profile")
        self.profile = profile
        self._client_factory = client_factory
        self._secret_reader = secret_reader

    def _client(self) -> genai.Client:
        if any(os.environ.get(name) for name in _PROXY_ENVIRONMENT):
            raise ModelPortError(ModelErrorCode.AMBIENT_PROXY)
        credential_failed = False
        try:
            credential = _validate_credential(self._secret_reader(GEMINI_SECRET_FILE))
        except Exception:
            credential_failed = True
            credential = ""
        if credential_failed:
            raise ModelPortError(ModelErrorCode.CREDENTIAL_UNAVAILABLE)
        construction_failed = False
        try:
            client = cast(
                genai.Client,
                self._client_factory(
                    vertexai=False,
                    api_key=credential,
                    http_options=types.HttpOptions(
                        base_url=self.profile.origin,
                        api_version=self.profile.api_version,
                        retry_options=None,
                    ),
                ),
            )
        except Exception:
            construction_failed = True
        if construction_failed:
            raise ModelPortError(ModelErrorCode.UNAVAILABLE)
        return client

    async def _generate(
        self,
        *,
        request: ModelIntentRequest | SlowQueryAdvisoryRequest,
        response_schema: type[ProviderIntentResponse] | type[ModelAdvisory],
        system_instruction: str,
        thinking_level: types.ThinkingLevel,
        max_output_tokens: int,
    ) -> IntentModelResult | AdvisoryModelResult:
        client = self._client()
        primary_error: BaseException | None = None
        mapped_error: ModelPortError | None = None
        result: IntentModelResult | AdvisoryModelResult | None = None
        try:
            response = await client.aio.models.generate_content(
                model=self.profile.model,
                contents=request.model_dump_json(),
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinking_level,
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            text = response.text
            if not isinstance(text, str):
                raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
            parsed = response_schema.model_validate_json(text)
            _require_clean_texts(parsed)
            usage = _model_usage(response)
            if isinstance(parsed, ProviderIntentResponse):
                result = IntentModelResult(
                    draft=IntentDraft(
                        intent=parsed.intent,
                        slots=parsed.slots.model_dump(exclude_none=True),
                        missing=parsed.missing,
                        confidence=parsed.confidence,
                        source=IntentSource.MODEL,
                    ),
                    usage=usage,
                )
            else:
                result = AdvisoryModelResult(advisory=parsed, usage=usage)
        except ValidationError as error:
            primary_error = error
            mapped_error = ModelPortError(ModelErrorCode.INVALID_RESPONSE)
        except Exception as error:
            primary_error = error
            mapped_error = _map_error(error)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            await _close_client(client, primary_error=primary_error)
        if mapped_error is not None:
            raise mapped_error
        if result is None:
            raise RuntimeError("unreachable Gemini response outcome")
        return result

    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        """请求一次严格意图 JSON，并由本地代码补可信来源。"""
        parsed = await self._generate(
            request=request,
            response_schema=ProviderIntentResponse,
            system_instruction=_SYSTEM_INTENT,
            thinking_level=types.ThinkingLevel[self.profile.intent_thinking_level],
            max_output_tokens=self.profile.intent_output_tokens,
        )
        if not isinstance(parsed, IntentModelResult):
            raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
        return parsed

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: StrictInt,
    ) -> AdvisoryModelResult:
        """请求一次慢查询建议；token budget 必须由 application 先收窄。"""
        limit = validate_advisory_output_tokens(max_output_tokens)
        parsed = await self._generate(
            request=request,
            response_schema=ModelAdvisory,
            system_instruction=_SYSTEM_ADVISORY,
            thinking_level=types.ThinkingLevel[self.profile.advisory_thinking_level],
            max_output_tokens=limit,
        )
        if not isinstance(parsed, AdvisoryModelResult):
            raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
        return parsed


__all__ = [
    "GEMINI_API_VERSION",
    "GEMINI_MODEL",
    "GEMINI_MODEL_PROFILE",
    "GEMINI_PROVIDER_ORIGIN",
    "GEMINI_SECRET_FILE",
    "GeminiModelAdapter",
]

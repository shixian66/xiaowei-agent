"""固定 Google Gemini Developer API 的单次请求 SDK adapter。"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Final, cast

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from xiaowei_agent.application.model_ports import validate_advisory_output_tokens
from xiaowei_agent.contracts import (
    IntentDraft,
    IntentSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelIntentRequest,
    ProviderIntentResponse,
    SlowQueryAdvisoryRequest,
    StrictInt,
)
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.interfaces import GEMINI_PROVIDER_ORIGIN
from xiaowei_agent.interfaces.secret_file import SecretFileError, read_secret_file
from xiaowei_agent.redaction import scrub_text

GEMINI_MODEL: Final[str] = "gemini-3-flash-preview"
GEMINI_API_VERSION: Final[str] = "v1beta"
GEMINI_SECRET_FILE: Final[str] = "/run/secrets/gemini_api_" + "key"
INTENT_OUTPUT_TOKEN_LIMIT: Final[int] = 2_048

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


class GeminiModelError(RuntimeError):
    """只携本地闭集错误码，不保留 provider 正文或 credential。"""

    def __init__(self, code: ModelErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _validate_credential(value: str) -> str:
    if not 20 <= len(value) <= 256 or value != value.strip() or any(
        character.isspace() or not character.isprintable() for character in value
    ):
        raise GeminiModelError(ModelErrorCode.CREDENTIAL_UNAVAILABLE)
    return value


def _require_clean_texts(value: ProviderIntentResponse | ModelAdvisory) -> None:
    if any(scrub_text(text) != text for text in model_text_values(value)):
        raise GeminiModelError(ModelErrorCode.INVALID_RESPONSE)


def _map_error(error: BaseException) -> GeminiModelError:
    if isinstance(error, GeminiModelError):
        return error
    if isinstance(error, TimeoutError):
        return GeminiModelError(ModelErrorCode.TIMEOUT)
    if isinstance(error, errors.APIError):
        if error.code == 401:
            return GeminiModelError(ModelErrorCode.UNAUTHORIZED)
        if error.code == 403:
            return GeminiModelError(ModelErrorCode.FORBIDDEN)
        if error.code == 429:
            return GeminiModelError(ModelErrorCode.RATE_LIMITED)
    return GeminiModelError(ModelErrorCode.UNAVAILABLE)


class GeminiModelAdapter:
    """只做本地 DTO 与一次 SDK request 的相互转换。"""

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] = genai.Client,
        secret_reader: Callable[[str], str] = read_secret_file,
    ) -> None:
        self._client_factory = client_factory
        self._secret_reader = secret_reader

    def _client(self) -> genai.Client:
        if any(os.environ.get(name) for name in _PROXY_ENVIRONMENT):
            raise GeminiModelError(ModelErrorCode.AMBIENT_PROXY)
        try:
            credential = _validate_credential(self._secret_reader(GEMINI_SECRET_FILE))
        except SecretFileError:
            raise GeminiModelError(ModelErrorCode.CREDENTIAL_UNAVAILABLE) from None
        try:
            return cast(
                genai.Client,
                self._client_factory(
                    vertexai=False,
                    api_key=credential,
                    http_options=types.HttpOptions(
                        base_url=GEMINI_PROVIDER_ORIGIN,
                        api_version=GEMINI_API_VERSION,
                        retry_options=None,
                    ),
                ),
            )
        except Exception as error:
            mapped = _map_error(error)
            raise mapped from None

    async def _generate(
        self,
        *,
        request: ModelIntentRequest | SlowQueryAdvisoryRequest,
        response_schema: type[ProviderIntentResponse] | type[ModelAdvisory],
        system_instruction: str,
        thinking_level: types.ThinkingLevel,
        max_output_tokens: int,
    ) -> ProviderIntentResponse | ModelAdvisory:
        client = self._client()
        primary_error: BaseException | None = None
        try:
            response = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=request.model_dump_json(),
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinking_level,
                    ),
                ),
            )
            text = response.text
            if not isinstance(text, str):
                raise GeminiModelError(ModelErrorCode.INVALID_RESPONSE)
            parsed = response_schema.model_validate_json(text)
            _require_clean_texts(parsed)
            return parsed
        except ValidationError as error:
            primary_error = error
            raise GeminiModelError(ModelErrorCode.INVALID_RESPONSE) from None
        except Exception as error:
            primary_error = error
            mapped = _map_error(error)
            raise mapped from None
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                await client.aio.aclose()
            except Exception as error:
                if primary_error is None:
                    mapped = _map_error(error)
                    raise mapped from None

    async def generate_intent(self, request: ModelIntentRequest) -> IntentDraft:
        """请求一次严格意图 JSON，并由本地代码补可信来源。"""
        parsed = await self._generate(
            request=request,
            response_schema=ProviderIntentResponse,
            system_instruction=_SYSTEM_INTENT,
            thinking_level=types.ThinkingLevel.LOW,
            max_output_tokens=INTENT_OUTPUT_TOKEN_LIMIT,
        )
        if not isinstance(parsed, ProviderIntentResponse):
            raise GeminiModelError(ModelErrorCode.INVALID_RESPONSE)
        return IntentDraft(
            intent=parsed.intent,
            slots=parsed.slots,
            missing=parsed.missing,
            confidence=parsed.confidence,
            source=IntentSource.MODEL,
        )

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: StrictInt,
    ) -> ModelAdvisory:
        """请求一次慢查询建议；token budget 必须由 application 先收窄。"""
        limit = validate_advisory_output_tokens(max_output_tokens)
        parsed = await self._generate(
            request=request,
            response_schema=ModelAdvisory,
            system_instruction=_SYSTEM_ADVISORY,
            thinking_level=types.ThinkingLevel.HIGH,
            max_output_tokens=limit,
        )
        if not isinstance(parsed, ModelAdvisory):
            raise GeminiModelError(ModelErrorCode.INVALID_RESPONSE)
        return parsed


__all__ = [
    "GEMINI_API_VERSION",
    "GEMINI_MODEL",
    "GEMINI_PROVIDER_ORIGIN",
    "GEMINI_SECRET_FILE",
    "GeminiModelAdapter",
    "GeminiModelError",
]

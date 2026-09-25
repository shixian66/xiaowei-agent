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
    IntentSource,
    InteractionClassifierRequest,
    InteractionDraft,
    InteractionModelResult,
    InteractionSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelInvocationProfile,
    ModelUsage,
    ProviderIntentResponse,
    ProviderInteractionResponse,
    SlowQueryAdvisoryRequest,
    StrictInt,
)
from xiaowei_agent.contracts.model import model_text_values
from xiaowei_agent.redaction import scrub_text

GEMINI_MODEL_PROFILE: Final[ModelInvocationProfile] = ModelInvocationProfile()
GEMINI_MODEL: Final[str] = GEMINI_MODEL_PROFILE.model
GEMINI_API_VERSION: Final[str] = GEMINI_MODEL_PROFILE.api_version
GEMINI_PROVIDER_ORIGIN: Final[str] = GEMINI_MODEL_PROFILE.origin
GEMINI_PROBE_OUTPUT_TOKENS: Final[int] = 16
"""连通性探针的输出上限。探针不读正文，只需换到一次真实回应；按 2048 上限作答
会让思考型模型写上数秒，越过 Web 探针的超时（本机实测 6–8 秒 → 约 1 秒）。"""
_CLIENT_CLOSE_TIMEOUT_SECONDS: Final[float] = 1.0

_PROXY_ENVIRONMENT: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_SYSTEM_INTERACTION: Final[str] = (
    "Return only the requested interaction JSON. Never choose a final capability, "
    "target, SQL, approval, tool, execution step, or next action."
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


def _require_clean_texts(
    value: ProviderInteractionResponse | ProviderIntentResponse | ModelAdvisory,
) -> None:
    if any(scrub_text(text) != text for text in model_text_values(value)):
        raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)


def _map_error(error: BaseException) -> ModelPortError:
    if isinstance(error, ModelPortError):
        return ModelPortError(error.code)
    if isinstance(error, TimeoutError | httpx.TimeoutException):
        return ModelPortError(ModelErrorCode.TIMEOUT)
    if isinstance(error, errors.APIError):
        code = error.code
        if type(code) is not int:
            return ModelPortError(ModelErrorCode.UNAVAILABLE)
        if code == 401:
            return ModelPortError(ModelErrorCode.UNAUTHORIZED)
        if code == 403:
            return ModelPortError(ModelErrorCode.FORBIDDEN)
        if code == 429:
            return ModelPortError(ModelErrorCode.RATE_LIMITED)
        if 500 <= code <= 599:
            return ModelPortError(ModelErrorCode.SERVER_ERROR)
        return ModelPortError(ModelErrorCode.UNAVAILABLE)
    if isinstance(error, httpx.TransportError | OSError):
        return ModelPortError(ModelErrorCode.TRANSPORT_ERROR)
    return ModelPortError(ModelErrorCode.UNAVAILABLE)


def _model_usage(response: types.GenerateContentResponse) -> ModelUsage:
    metadata = response.usage_metadata
    if metadata is None:
        return ModelUsage()
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
    close_task = asyncio.create_task(client.aio.aclose())
    deadline = asyncio.get_running_loop().time() + _CLIENT_CLOSE_TIMEOUT_SECONDS
    pending_cancellation: asyncio.CancelledError | None = None
    close_error: BaseException | None = None
    try:
        while not close_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                close_error = TimeoutError("Gemini client close timed out")
                break
            try:
                async with asyncio.timeout(remaining):
                    await asyncio.shield(close_task)
            except asyncio.CancelledError as error:
                if close_task.cancelled():
                    close_error = error
                    break
                pending_cancellation = error
            except BaseException as error:
                close_error = error
                break
        if close_error is None and close_task.done():
            try:
                close_task.result()
            except BaseException as error:
                close_error = error
    finally:
        if not close_task.done():
            close_task.cancel()
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as error:
                    if close_task.done():
                        break
                    pending_cancellation = error
                except BaseException as error:
                    if close_error is None:
                        close_error = error
                    break
            try:
                close_task.result()
            except BaseException as error:
                if close_error is None:
                    close_error = error
    if pending_cancellation is not None:
        raise pending_cancellation
    if close_error is None or primary_error is not None:
        return
    if isinstance(close_error, TimeoutError | httpx.TimeoutException):
        raise ModelPortError(ModelErrorCode.TIMEOUT)
    raise ModelPortError(ModelErrorCode.UNAVAILABLE)


def _build_client(
    *,
    api_key: str,
    profile: ModelInvocationProfile,
    client_factory: Callable[..., Any],
) -> genai.Client:
    """构造一个只指向固定 origin / api_version 的 client。

    ``GeminiModelAdapter`` 与连接探针共用这一处：探针若自己再建一次 client，
    环境代理拒绝、凭据形状检查与固定 base_url 就变成两份，"探针通过但真实调用
    失败"只是时间问题。
    """
    if any(os.environ.get(name) for name in _PROXY_ENVIRONMENT):
        raise ModelPortError(ModelErrorCode.AMBIENT_PROXY)
    credential_failed = False
    try:
        credential = _validate_credential(api_key)
    except Exception:
        credential_failed = True
        credential = ""
    if credential_failed:
        raise ModelPortError(ModelErrorCode.CREDENTIAL_UNAVAILABLE)
    construction_failed = False
    try:
        client = cast(
            genai.Client,
            client_factory(
                vertexai=False,
                api_key=credential,
                http_options=types.HttpOptions(
                    base_url=profile.origin,
                    api_version=profile.api_version,
                    retry_options=None,
                ),
            ),
        )
    except Exception:
        construction_failed = True
    if construction_failed:
        raise ModelPortError(ModelErrorCode.UNAVAILABLE)
    return client


class GeminiModelAdapter:
    """只做本地 DTO 与一次 SDK request 的相互转换。"""

    def __init__(
        self,
        *,
        api_key: str,
        profile: ModelInvocationProfile = GEMINI_MODEL_PROFILE,
        client_factory: Callable[..., Any] = genai.Client,
    ) -> None:
        # 正常 Pydantic 构造只能得到这一组 Literal；这里还拒绝 Python 层可造出的
        # object.__setattr__ 变体，避免 profile 注入口绕过固定 origin/model 边界。
        if profile != GEMINI_MODEL_PROFILE:
            raise ValueError("Gemini adapter requires the fixed RI3 profile")
        self.profile = profile
        self._client_factory = client_factory
        # Key 由装配层从 AI 域 `config.json` 注入；adapter 不持有任何路径常量，
        # 也不再自己去文件系统找凭据。
        self._api_key = api_key

    def _client(self) -> genai.Client:
        return _build_client(
            api_key=self._api_key,
            profile=self.profile,
            client_factory=self._client_factory,
        )

    async def _generate(
        self,
        *,
        request: InteractionClassifierRequest | SlowQueryAdvisoryRequest,
        response_schema: type[ProviderInteractionResponse] | type[ModelAdvisory],
        system_instruction: str,
        thinking_level: types.ThinkingLevel,
        max_output_tokens: int,
    ) -> InteractionModelResult | AdvisoryModelResult:
        client = self._client()
        primary_error: BaseException | None = None
        mapped_error: ModelPortError | None = None
        result: InteractionModelResult | AdvisoryModelResult | None = None
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
            if isinstance(parsed, ProviderInteractionResponse):
                capability = parsed.capability
                result = InteractionModelResult(
                    draft=InteractionDraft(
                        proposed_kind=parsed.proposed_kind,
                        capability_draft=(
                            None
                            if capability is None
                            else IntentDraft(
                                intent=capability.intent,
                                slots=capability.slots.model_dump(exclude_none=True),
                                missing=capability.missing,
                                confidence=capability.confidence,
                                source=IntentSource.MODEL,
                            )
                        ),
                        confidence=parsed.confidence,
                        source=InteractionSource.MODEL,
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

    async def classify(
        self, request: InteractionClassifierRequest
    ) -> InteractionModelResult:
        """请求一次严格 interaction JSON，并由本地代码补可信来源。"""
        parsed = await self._generate(
            request=request,
            response_schema=ProviderInteractionResponse,
            system_instruction=_SYSTEM_INTERACTION,
            thinking_level=types.ThinkingLevel[self.profile.interaction_thinking_level],
            max_output_tokens=self.profile.interaction_output_tokens,
        )
        if not isinstance(parsed, InteractionModelResult):
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


async def probe_connection(
    *,
    api_key: str,
    contents: str,
    profile: ModelInvocationProfile = GEMINI_MODEL_PROFILE,
    client_factory: Callable[..., Any] = genai.Client,
) -> None:
    """发一次最小的固定请求验证凭据与连通性；失败抛 ``ModelPortError``。

    **不经过 ``InteractionClassifierPort``**：那条路带着 system instruction、response schema
    与 thinking 配置，测的是"模型能不能按约定作答"。控制面要回答的是更前面一个
    问题——"这把 Key 现在通不通"。用后者的失败去解释前者只会误导管理员。

    响应正文一律不读、不返回：调用方只需要知道"有没有换到一次真实回应"。
    """
    client = _build_client(
        api_key=api_key, profile=profile, client_factory=client_factory
    )
    primary_error: BaseException | None = None
    mapped_error: ModelPortError | None = None
    try:
        response = await client.aio.models.generate_content(
            model=profile.model,
            contents=contents,
            config=types.GenerateContentConfig(
                max_output_tokens=GEMINI_PROBE_OUTPUT_TOKENS,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        if response is None:
            raise ModelPortError(ModelErrorCode.INVALID_RESPONSE)
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


__all__ = [
    "GEMINI_API_VERSION",
    "GEMINI_MODEL",
    "GEMINI_MODEL_PROFILE",
    "GEMINI_PROBE_OUTPUT_TOKENS",
    "GEMINI_PROVIDER_ORIGIN",
    "GeminiModelAdapter",
    "probe_connection",
]

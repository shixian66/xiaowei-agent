"""Application 层可见的两个窄模型端口。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Protocol

from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentModelResult,
    InteractionClassifierRequest,
    InteractionModelResult,
    ModelErrorCode,
    ModelFallbackCode,
    ModelIntentRequest,
    SlowQueryAdvisoryRequest,
    StrictInt,
)

ADVISORY_OUTPUT_TOKEN_LIMIT: Final[int] = 4_000
INTENT_RETRYABLE_ERROR_CODES: Final[frozenset[ModelErrorCode]] = frozenset(
    {
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.SERVER_ERROR,
        ModelErrorCode.TRANSPORT_ERROR,
    }
)
MODEL_ERROR_FALLBACK_CODES: Final[Mapping[ModelErrorCode, ModelFallbackCode]] = (
    MappingProxyType(
        {
            ModelErrorCode.CREDENTIAL_UNAVAILABLE: (
                ModelFallbackCode.CREDENTIAL_UNAVAILABLE
            ),
            ModelErrorCode.AMBIENT_PROXY: ModelFallbackCode.AMBIENT_PROXY,
            ModelErrorCode.UNAUTHORIZED: ModelFallbackCode.UNAUTHORIZED,
            ModelErrorCode.FORBIDDEN: ModelFallbackCode.FORBIDDEN,
            ModelErrorCode.RATE_LIMITED: ModelFallbackCode.RATE_LIMITED,
            ModelErrorCode.SERVER_ERROR: ModelFallbackCode.SERVER_ERROR,
            ModelErrorCode.TRANSPORT_ERROR: ModelFallbackCode.TRANSPORT_ERROR,
            ModelErrorCode.TIMEOUT: ModelFallbackCode.PROVIDER_TIMEOUT,
            ModelErrorCode.INVALID_RESPONSE: ModelFallbackCode.INVALID_RESPONSE,
            ModelErrorCode.UNAVAILABLE: ModelFallbackCode.UNAVAILABLE,
        }
    )
)


class ModelPortError(RuntimeError):
    """只携 provider-neutral 闭集错误码的安全端口异常。"""

    def __init__(self, code: ModelErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def fallback_code_for_model_error(code: object) -> ModelFallbackCode:
    """总函数：已知 provider 错误显式映射，未知值安全降级。"""
    if not isinstance(code, ModelErrorCode):
        return ModelFallbackCode.UNAVAILABLE
    return MODEL_ERROR_FALLBACK_CODES.get(code, ModelFallbackCode.UNAVAILABLE)


def validate_advisory_output_tokens(value: StrictInt) -> StrictInt:
    """拒绝 bool 与 profile 上限之外的 advisory token budget。"""
    if type(value) is not int or not 1 <= value <= ADVISORY_OUTPUT_TOKEN_LIMIT:
        raise ValueError("max_output_tokens must be an integer from 1 to 4000")
    return value


class IntentModelPort(Protocol):
    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        """返回草案与 usage；失败时抛安全的 ``ModelPortError``。"""
        ...


class InteractionClassifierPort(Protocol):
    async def classify(
        self, request: InteractionClassifierRequest
    ) -> InteractionModelResult:
        """返回交互候选与 usage；失败时抛安全的 ``ModelPortError``。"""
        ...


class SlowQueryAdvisoryPort(Protocol):
    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: StrictInt,
    ) -> AdvisoryModelResult:
        """返回建议与 usage；失败时抛安全的 ``ModelPortError``。"""
        ...


__all__ = [
    "ADVISORY_OUTPUT_TOKEN_LIMIT",
    "INTENT_RETRYABLE_ERROR_CODES",
    "MODEL_ERROR_FALLBACK_CODES",
    "IntentModelPort",
    "InteractionClassifierPort",
    "ModelPortError",
    "SlowQueryAdvisoryPort",
    "fallback_code_for_model_error",
    "validate_advisory_output_tokens",
]

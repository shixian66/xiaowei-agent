"""Application 层可见的两个窄模型端口。"""

from __future__ import annotations

from typing import Final, Protocol

from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentModelResult,
    ModelErrorCode,
    ModelIntentRequest,
    SlowQueryAdvisoryRequest,
    StrictInt,
)

ADVISORY_OUTPUT_TOKEN_LIMIT = 4_000
INTENT_RETRYABLE_ERROR_CODES: Final[frozenset[ModelErrorCode]] = frozenset(
    {
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.SERVER_ERROR,
        ModelErrorCode.TRANSPORT_ERROR,
    }
)


class ModelPortError(RuntimeError):
    """只携 provider-neutral 闭集错误码的安全端口异常。"""

    def __init__(self, code: ModelErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def validate_advisory_output_tokens(value: StrictInt) -> StrictInt:
    """拒绝 bool 与 profile 上限之外的 advisory token budget。"""
    if type(value) is not int or not 1 <= value <= ADVISORY_OUTPUT_TOKEN_LIMIT:
        raise ValueError("max_output_tokens must be an integer from 1 to 4000")
    return value


class IntentModelPort(Protocol):
    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        """返回草案与 usage；失败时抛安全的 ``ModelPortError``。"""
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
    "IntentModelPort",
    "ModelPortError",
    "SlowQueryAdvisoryPort",
    "validate_advisory_output_tokens",
]

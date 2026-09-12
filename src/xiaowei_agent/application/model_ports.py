"""Application 层可见的两个窄模型端口。"""

from __future__ import annotations

from typing import Protocol

from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentModelResult,
    ModelIntentRequest,
    SlowQueryAdvisoryRequest,
    StrictInt,
)

ADVISORY_OUTPUT_TOKEN_LIMIT = 4_000


def validate_advisory_output_tokens(value: StrictInt) -> StrictInt:
    """拒绝 bool 与 profile 上限之外的 advisory token budget。"""
    if type(value) is not int or not 1 <= value <= ADVISORY_OUTPUT_TOKEN_LIMIT:
        raise ValueError("max_output_tokens must be an integer from 1 to 4000")
    return value


class IntentModelPort(Protocol):
    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        """返回不可信意图草案与同次可信 usage。"""
        ...


class SlowQueryAdvisoryPort(Protocol):
    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: StrictInt,
    ) -> AdvisoryModelResult:
        """返回只读建议与同次可信 usage，不产生执行动作。"""
        ...


__all__ = [
    "ADVISORY_OUTPUT_TOKEN_LIMIT",
    "IntentModelPort",
    "SlowQueryAdvisoryPort",
    "validate_advisory_output_tokens",
]

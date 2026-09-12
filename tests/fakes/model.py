"""两个窄模型端口的离线 fake。"""

from __future__ import annotations

from xiaowei_agent.application.model_ports import (
    ModelPortError,
    validate_advisory_output_tokens,
)
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentModelResult,
    ModelErrorCode,
    ModelIntentRequest,
    SlowQueryAdvisoryRequest,
    StrictInt,
)


class ScriptedModelAdapter:
    def __init__(
        self, *, intent: IntentModelResult, advisory: AdvisoryModelResult
    ) -> None:
        self.intent = intent
        self.advisory = advisory
        self.intent_requests: list[ModelIntentRequest] = []
        self.advisory_requests: list[tuple[SlowQueryAdvisoryRequest, int]] = []

    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        self.intent_requests.append(request)
        return self.intent

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: StrictInt,
    ) -> AdvisoryModelResult:
        limit = validate_advisory_output_tokens(max_output_tokens)
        self.advisory_requests.append((request, limit))
        return self.advisory


def assert_safe_model_port_error(
    error: ModelPortError, expected: ModelErrorCode
) -> None:
    """断言端口错误没有 provider detail 或 Python 异常链。"""
    assert error.code is expected
    assert str(error) == expected.value
    assert error.__cause__ is None
    assert error.__context__ is None


__all__ = ["ScriptedModelAdapter", "assert_safe_model_port_error"]

"""两个窄模型端口的离线 fake。"""

from __future__ import annotations

from xiaowei_agent.application.model_ports import (
    ModelPortError,
    validate_advisory_output_tokens,
)
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    InteractionClassifierRequest,
    InteractionModelResult,
    ModelErrorCode,
    SlowQueryAdvisoryRequest,
    StrictInt,
)


class ScriptedModelAdapter:
    def __init__(
        self, *, interaction: InteractionModelResult, advisory: AdvisoryModelResult
    ) -> None:
        self.interaction = interaction
        self.advisory = advisory
        self.interaction_requests: list[InteractionClassifierRequest] = []
        self.advisory_requests: list[tuple[SlowQueryAdvisoryRequest, int]] = []

    async def classify(
        self, request: InteractionClassifierRequest
    ) -> InteractionModelResult:
        self.interaction_requests.append(request)
        return self.interaction

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

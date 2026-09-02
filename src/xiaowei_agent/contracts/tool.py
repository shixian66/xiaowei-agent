"""工具调用与工具结果。

``ToolResult`` 的构造受**模块级凭证**保护：``model_validator(mode="before")``
要求 payload 携带只有 ``tools.gateway`` 能拿到的哨兵对象。``model_construct`` 已由
:class:`Contract` 基类封死；``model_copy`` 在此进一步禁止——基类的版本会重新校验，
但重新校验后的副本不再由 Gateway 签发，来源无从追溯。

**诚实边界**：Python 无法提供语言级私有性。``object.__setattr__`` 与重定义模块两条
路径无法封堵，属已知残余风险，由评审与源码扫描测试覆盖（ARCHITECTURE §5.8）。
"""

from collections.abc import Mapping
from typing import Any, Final, Never

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import (
    Contract,
    FiniteFloat,
    FrozenMap,
    StrictStr,
    TraceId,
)
from xiaowei_agent.contracts.enums import ToolCallStatus
from xiaowei_agent.contracts.errors import AgentError

_TOOL_RESULT_WITNESS: Final[object] = object()
_WITNESS_KEY: Final[str] = "__gateway_witness__"


class ToolCall(Contract):
    gateway: StrictStr
    operation: StrictStr
    step_id: StrictStr
    typed_args: FrozenMap
    timeout_seconds: FiniteFloat = Field(gt=0.0, le=300.0)
    idempotency_key: StrictStr


class ToolResult(Contract):
    status: ToolCallStatus
    data_view: tuple[FrozenMap, ...]
    raw_ref: str | None
    source: StrictStr
    limitations: tuple[str, ...]
    trace_id: TraceId
    error: AgentError | None = None

    @model_validator(mode="before")
    @classmethod
    def _require_gateway_witness(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError("ToolResult 只能由 ToolGateway 构造")
        if data.pop(_WITNESS_KEY, None) is not _TOOL_RESULT_WITNESS:
            raise ValueError("ToolResult 只能由 ToolGateway 构造")
        return data

    @classmethod
    def model_construct(cls, *args: object, **kwargs: object) -> Never:
        # 基类已统一封死；此处覆盖只为给出更准确的错误信息。
        raise NotImplementedError("ToolResult 只能由 ToolGateway 构造")

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Never:
        # 签名与 Contract.model_copy 对齐，否则 mypy strict 报不兼容覆盖。
        # 这里比基类更严：复制出的结果不再由 Gateway 签发，来源无从追溯。
        raise NotImplementedError("ToolResult 不可复制；请由 ToolGateway 重新生成")

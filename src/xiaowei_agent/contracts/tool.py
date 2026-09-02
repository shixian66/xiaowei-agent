"""工具调用契约。

``ToolResult`` 及其构造凭证在 T8 加入本模块——两者共享同一套构造边界，分开放会让
"结果只能由 Gateway 签发"这条约束跨文件失联。
"""

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, FiniteFloat, FrozenMap, StrictStr


class ToolCall(Contract):
    gateway: StrictStr
    operation: StrictStr
    step_id: StrictStr
    typed_args: FrozenMap
    timeout_seconds: FiniteFloat = Field(gt=0.0, le=300.0)
    idempotency_key: StrictStr

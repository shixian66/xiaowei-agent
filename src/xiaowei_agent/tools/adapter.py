"""adapter 的内部返回类型与契约。

``AdapterResponse`` **不含** ``side_effect`` / ``effect_class``：adapter 在类型
层面就无法降级分类（ADR-007 D7 承重断言 2），比运行时拒绝更强。

``error`` 的类型是 ``ExternalContent``，因此第三方错误文本恒为不可信外部内容，
不会被当作可信控制信号（ARCHITECTURE §9）。
"""

from typing import Protocol

from pydantic import Field

from xiaowei_agent.contracts import (
    AdapterStatus,
    Contract,
    ExternalContent,
    FrozenMap,
    RequestContext,
    StrictInt,
    StrictStr,
    ToolCall,
)


class AdapterResponse(Contract):
    status: AdapterStatus
    payload: tuple[FrozenMap, ...]
    source: StrictStr
    error: ExternalContent | None = None
    elapsed_ms: StrictInt = Field(ge=0)


class ToolAdapter(Protocol):
    async def execute(self, call: ToolCall, *, context: RequestContext) -> AdapterResponse: ...

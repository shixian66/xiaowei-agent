"""adapter 的内部返回类型与契约。

``AdapterResponse`` **不含** ``side_effect`` / ``effect_class``：adapter 在类型
层面就无法降级分类（ADR-007 D7 承重断言 2），比运行时拒绝更强。

``error`` 的类型是 ``ExternalContent``，因此第三方错误文本恒为不可信外部内容，
不会被当作可信控制信号（ARCHITECTURE §9）。
"""

from typing import Protocol, Self

from pydantic import Field, model_validator

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

    @model_validator(mode="after")
    def _status_matches_payload_and_error(self) -> Self:
        """拒绝自相矛盾的组合，不让 Gateway 去猜。

        ``status=OK`` 却带 ``error`` 时，Gateway 会返回成功结果并**静默丢掉**
        错误；``status`` 非 OK 却带 ``payload`` 时，半截数据会被当成证据。两种
        矛盾都必须在契约层拒绝。

        ``error`` 对非 OK 状态仍是可选：并非每种失败都产出文本（例如超时）。
        此时 Gateway 的 mapper 会给出 ``cause_ref=None``，这是明确的"无原文"
        而不是丢失。
        """
        if self.status is AdapterStatus.OK:
            if self.error is not None:
                raise ValueError("an OK adapter response must not carry an error")
        elif self.payload:
            raise ValueError("a failed adapter response must not carry a payload")
        return self


class ToolAdapter(Protocol):
    async def execute(self, call: ToolCall, *, context: RequestContext) -> AdapterResponse: ...

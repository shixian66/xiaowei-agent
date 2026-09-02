"""模型产出的意图草案。

**这是全项目不可信度最高的 DTO**：字段集刻意只有五项且 ``extra="forbid"``，
模型无法通过多写字段来影响 capability、目标、SQL、权限或审批（ARCHITECTURE
§4.1、ADR-007 D7）。槽位取值限定为字符串标量——嵌套结构会成为夹带任意载荷的
通道。
"""

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, FrozenStrMap, StrictStr
from xiaowei_agent.contracts.enums import IntentSource


class IntentDraft(Contract):
    intent: StrictStr
    slots: FrozenStrMap
    missing: tuple[StrictStr, ...]
    # 显式 allow_inf_nan=False：靠 ge/le 隐式挡住 NaN 依赖"NaN 比较恒为假"这一
    # 间接性质，读者无从看出意图，也挡不住将来放宽边界时重新引入。
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source: IntentSource

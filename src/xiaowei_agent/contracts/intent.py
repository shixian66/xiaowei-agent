"""模型产出的意图草案与当前意图槽位闭集。

**这是全项目不可信度最高的 DTO**：字段集刻意只有五项且 ``extra="forbid"``，
模型无法通过多写字段来影响 capability、目标、SQL、权限或审批（ARCHITECTURE
§4.1、ADR-007 D7）。槽位取值限定为字符串标量——嵌套结构会成为夹带任意载荷的
通道。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, FrozenStrMap, StrictStr
from xiaowei_agent.contracts.enums import IntentSource

SLOW_QUERY_INTENT: Final[str] = "starrocks.slow_query.diagnose"
PROMETHEUS_ALERT_INTENT: Final[str] = "prometheus.alert.evidence"
ASSET_INVENTORY_INTENT: Final[str] = "asset.inventory.lookup"
UNKNOWN_INTENT: Final[str] = "unknown"

INTENT_SLOT_ALLOWLISTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        SLOW_QUERY_INTENT: frozenset(
            {
                "environment_id",
                "database",
                "user_name",
                "query_id",
                "window_minutes",
            }
        ),
        PROMETHEUS_ALERT_INTENT: frozenset(
            {"alert_name", "instance", "fingerprint", "window_minutes"}
        ),
        ASSET_INVENTORY_INTENT: frozenset({"asset_id", "hostname", "ip"}),
        UNKNOWN_INTENT: frozenset(),
    }
)
"""规则与模型 provider 响应共同使用的 intent-specific 槽位真源。"""

INTENT_MISSING_ALLOWLISTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        SLOW_QUERY_INTENT: frozenset({"environment_id"}),
        PROMETHEUS_ALERT_INTENT: frozenset({"alert_name", "instance"}),
        ASSET_INVENTORY_INTENT: frozenset({"asset_selector"}),
        UNKNOWN_INTENT: frozenset(),
    }
)
"""模型可以声明缺失的 intent-specific 输入名闭集。"""


class IntentDraft(Contract):
    intent: StrictStr
    slots: FrozenStrMap
    missing: tuple[StrictStr, ...]
    # 显式 allow_inf_nan=False：靠 ge/le 隐式挡住 NaN 依赖"NaN 比较恒为假"这一
    # 间接性质，读者无从看出意图，也挡不住将来放宽边界时重新引入。
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source: IntentSource

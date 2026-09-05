"""M6a PromQL 模板闭集。"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

CPU_PERCENT_V1: Final[str] = "prometheus.alert.host_cpu_percent.v1"
INSTANCE_UP_V1: Final[str] = "prometheus.alert.instance_up.v1"

_ALERT_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {"HostHighCpu": CPU_PERCENT_V1, "InstanceDown": INSTANCE_UP_V1}
)


def template_for_alert(alert_name: str) -> str:
    """返回告警名的唯一模板；未注册名称 fail-closed。"""
    try:
        return _ALERT_TEMPLATES[alert_name]
    except KeyError:
        raise ValueError("alert_name has no registered template") from None


def escape_promql_label_value(value: str) -> str:
    """按 PromQL 字符串字面量规则确定性转义标签值。"""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

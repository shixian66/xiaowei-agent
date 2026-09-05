"""M6a PromQL 模板闭集。"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from xiaowei_agent.capabilities.prometheus_alert import (
    CPU_PERCENT_TEMPLATE_V1,
    INSTANCE_UP_TEMPLATE_V1,
)

CPU_PERCENT_V1: Final[str] = CPU_PERCENT_TEMPLATE_V1
INSTANCE_UP_V1: Final[str] = INSTANCE_UP_TEMPLATE_V1

_ALERT_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {"HostHighCpu": CPU_PERCENT_V1, "InstanceDown": INSTANCE_UP_V1}
)
_TEMPLATE_METRICS: Final[Mapping[str, str]] = MappingProxyType(
    {CPU_PERCENT_V1: "node_cpu_percent", INSTANCE_UP_V1: "up"}
)


def template_for_alert(alert_name: str) -> str:
    """返回告警名的唯一模板；未注册名称 fail-closed。"""
    try:
        return _ALERT_TEMPLATES[alert_name]
    except KeyError:
        raise ValueError("alert_name has no registered template") from None


def metric_name_for_template(template_id: str) -> str:
    """返回模板证据允许携带的唯一 metric_name。"""
    try:
        return _TEMPLATE_METRICS[template_id]
    except KeyError:
        raise ValueError("template has no registered metric") from None


def escape_promql_label_value(value: str) -> str:
    """按 PromQL 字符串字面量规则确定性转义标签值。"""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

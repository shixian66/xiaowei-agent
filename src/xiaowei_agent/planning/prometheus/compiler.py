"""从闭集参数与模板生成确定性的 PromQL。"""

import datetime as dt

from xiaowei_agent.contracts import PromqlSurface
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.templates import (
    CPU_PERCENT_V1,
    INSTANCE_UP_V1,
    escape_promql_label_value,
    template_for_alert,
)


def compile_promql(
    *, template_id: str, params: PrometheusAlertParams, surface: PromqlSurface
) -> str:
    """编译一个已注册模板，并同时执行声明预算检查。"""
    if template_id not in surface.allowed_template_ids:
        raise ValueError("template is outside the declared surface")
    if template_for_alert(params.alert_name) != template_id:
        raise ValueError("template does not match alert_name")
    window = params.window_end - params.window_start
    points = int(window.total_seconds()) // params.step_seconds + 1
    if window > dt.timedelta(minutes=surface.max_window_minutes):
        raise ValueError("window exceeds the declared surface")
    if params.max_series > surface.max_series:
        raise ValueError("series budget exceeds the declared surface")
    if points > surface.max_points_per_series:
        raise ValueError("point budget exceeds the declared surface")
    instance = escape_promql_label_value(params.instance)
    if template_id == CPU_PERCENT_V1:
        return (
            "100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{"
            f'mode="idle",instance="{instance}"}}[5m])))'
        )
    if template_id == INSTANCE_UP_V1:
        return f'up{{instance="{instance}"}}'
    raise ValueError("template is not implemented")

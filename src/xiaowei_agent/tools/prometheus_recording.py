"""M6a 内置 Prometheus synthetic recording；不触达外部系统。"""

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.contracts import AdapterStatus
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingKey

IS_FAKE: Final[bool] = True
_SOURCE: Final[str] = "prometheus-recording"


def default_prometheus_recording(
    *,
    operation: str,
    promql: str,
    template_id: str,
    alert_name: str = "HostHighCpu",
    instance: str = "node-1.example.com:9100",
    metric_name: str = "node_cpu_percent",
    values: tuple[float, float] = (82.0, 91.5),
    window_start: str = "2026-09-05T11:30:00+00:00",
    window_end: str = "2026-09-05T12:00:00+00:00",
) -> Mapping[PrometheusRecordingKey, AdapterResponse]:
    """生成固定样例；查询、模板与证据身份由装配根显式注入。"""
    start = dt.datetime.fromisoformat(window_start)
    end = dt.datetime.fromisoformat(window_end)
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or end <= start
        or end - start != dt.timedelta(minutes=30)
    ):
        raise ValueError("recording window must be an aware 30 minute range")
    if len(values) != 2:
        raise ValueError("recording requires exactly two metric values")
    rows: tuple[Mapping[str, Any], ...] = (
        {
            "series_key": f"instance={instance}",
            "metric_name": metric_name,
            "instance": instance,
            "timestamp_ms": int(start.timestamp() * 1000),
            "value": values[0],
        },
        {
            "series_key": f"instance={instance}",
            "metric_name": metric_name,
            "instance": instance,
            "timestamp_ms": int(end.timestamp() * 1000),
            "value": values[1],
        },
    )
    key: PrometheusRecordingKey = (
        "dev-local",
        "dev",
        operation,
        promql,
        template_id,
        alert_name,
        instance,
        start.isoformat(),
        end.isoformat(),
        60,
        5,
        361,
    )
    return {
        key: AdapterResponse(
            status=AdapterStatus.OK,
            payload=rows,
            source=_SOURCE,
            error=None,
            elapsed_ms=1,
        )
    }

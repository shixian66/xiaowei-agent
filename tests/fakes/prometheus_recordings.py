"""Prometheus 告警证据闭环的合成 recordings。"""

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.contracts import AdapterStatus, ExternalContent, ExternalSource
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingKey
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingKey

ALERT_OPERATION: Final[str] = "get_active_alerts"
METRIC_OPERATION: Final[str] = "query_metric_range"
ALERT_NAME: Final[str] = "HostHighCpu"
INSTANCE: Final[str] = "node-1.example.com:9100"
FINGERPRINT: Final[str] = "fp-001"
START: Final[str] = "2026-09-05T11:30:00+00:00"
END: Final[str] = "2026-09-05T12:00:00+00:00"
TEMPLATE_ID: Final[str] = "prometheus.alert.host_cpu_percent.v1"
PROMQL: Final[str] = (
    '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
    'instance="node-1.example.com:9100"}[5m])))'
)

AlertRecording = Mapping[AlertmanagerRecordingKey, Any]
MetricRecording = Mapping[PrometheusRecordingKey, Any]


def _alert_key(*, fingerprint: str | None = None) -> AlertmanagerRecordingKey:
    return (
        "dev-local",
        "dev",
        ALERT_OPERATION,
        ALERT_NAME,
        INSTANCE,
        fingerprint,
        5,
    )


def _metric_key() -> PrometheusRecordingKey:
    return (
        "dev-local",
        "dev",
        METRIC_OPERATION,
        PROMQL,
        TEMPLATE_ID,
        ALERT_NAME,
        INSTANCE,
        START,
        END,
        60,
        5,
        361,
    )


def alert_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fingerprint": FINGERPRINT,
        "state": "firing",
        "alert_name": ALERT_NAME,
        "instance": INSTANCE,
        "severity": "warning",
        "starts_at": "2026-09-05T11:20:00+00:00",
        "ends_at": None,
        "silenced": False,
        "inhibited": False,
    }
    return row | overrides


def metric_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "series_key": f"instance={INSTANCE}",
        "metric_name": "node_cpu_percent",
        "instance": INSTANCE,
        "timestamp_ms": 1_788_607_800_000,
        "value": 82.0,
    }
    return row | overrides


def _ok(*rows: Mapping[str, object], source: str) -> AdapterResponse:
    return AdapterResponse(
        status=AdapterStatus.OK,
        payload=tuple(rows),
        source=source,
        error=None,
        elapsed_ms=1,
    )


def _timeout(*, source: str) -> AdapterResponse:
    return AdapterResponse(
        status=AdapterStatus.TIMEOUT,
        payload=(),
        source=source,
        error=None,
        elapsed_ms=30_000,
    )


def recordings_for(
    scenario: str,
) -> tuple[AlertRecording, MetricRecording]:
    """返回指定 L2 情形的精确双网关 recording。"""
    alerts: tuple[Mapping[str, object], ...] = (alert_row(),)
    metrics: tuple[Mapping[str, object], ...] = (
        metric_row(),
        metric_row(timestamp_ms=1_788_609_600_000, value=91.5),
    )
    alert_response: Any = _ok(*alerts, source="alertmanager-recording")
    metric_response: Any = _ok(*metrics, source="prometheus-recording")
    if scenario == "silenced":
        alert_response = _ok(
            alert_row(silenced=True), source="alertmanager-recording"
        )
    elif scenario == "empty_metric":
        metric_response = _ok(source="prometheus-recording")
    elif scenario == "empty_alert":
        alert_response = _ok(source="alertmanager-recording")
    elif scenario == "multiple_alerts":
        alert_response = _ok(
            alert_row(),
            alert_row(fingerprint="fp-002"),
            source="alertmanager-recording",
        )
    elif scenario == "alert_timeout":
        alert_response = _timeout(source="alertmanager-recording")
    elif scenario == "prometheus_timeout":
        metric_response = _timeout(source="prometheus-recording")
    elif scenario == "malformed":
        metric_response = _ok(
            metric_row(metric_name="unexpected_metric"),
            source="prometheus-recording",
        )
    elif scenario == "polluted":
        upstream = "token" + "=" + "synthetic-value"
        alert_response = _ok(
            alert_row(annotation=upstream, generator_url="https://invalid.local"),
            source="alertmanager-recording",
        )
        metric_response = _ok(
            *metrics,
            source="prometheus-recording",
        )
    elif scenario == "alert_error":
        marker = "password" + "=" + "synthetic-upstream"
        alert_response = AdapterResponse(
            status=AdapterStatus.ERROR,
            payload=(),
            source="alertmanager-recording",
            error=ExternalContent.capture(
                source=ExternalSource.TOOL,
                content=marker,
                captured_at=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
            ),
            elapsed_ms=2,
        )
    elif scenario != "golden":
        raise ValueError("unknown Prometheus recording scenario")
    return ({_alert_key(): alert_response}, {_metric_key(): metric_response})

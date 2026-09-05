"""M6a 内置 Alertmanager synthetic recording；不触达外部系统。"""

from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.contracts import AdapterStatus
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingKey

IS_FAKE: Final[bool] = True
_SOURCE: Final[str] = "alertmanager-recording"


def _response(
    *, alert_name: str, instance: str, fingerprint: str, severity: str
) -> AdapterResponse:
    row: Mapping[str, Any] = {
        "fingerprint": fingerprint,
        "state": "firing",
        "alert_name": alert_name,
        "instance": instance,
        "severity": severity,
        "starts_at": "2026-09-05T11:20:00+00:00",
        "ends_at": None,
        "silenced": False,
        "inhibited": False,
    }
    return AdapterResponse(
        status=AdapterStatus.OK,
        payload=(row,),
        source=_SOURCE,
        error=None,
        elapsed_ms=1,
    )


def default_alertmanager_recording(
    *, operation: str
) -> Mapping[AlertmanagerRecordingKey, AdapterResponse]:
    """返回三个固定样例，operation 由 composition root 显式注入。"""
    host = _response(
        alert_name="HostHighCpu",
        instance="node-1.example.com:9100",
        fingerprint="fp-001",
        severity="warning",
    )
    down = _response(
        alert_name="InstanceDown",
        instance="10.0.0.8:9100",
        fingerprint="fp-002",
        severity="critical",
    )
    short_host = _response(
        alert_name="HostHighCpu",
        instance="node-1:9100",
        fingerprint="fp-003",
        severity="warning",
    )
    return {
        (
            "dev-local",
            "dev",
            operation,
            "HostHighCpu",
            "node-1.example.com:9100",
            None,
            5,
        ): host,
        (
            "dev-local",
            "dev",
            operation,
            "HostHighCpu",
            "node-1.example.com:9100",
            "fp-001",
            5,
        ): host,
        (
            "dev-local",
            "dev",
            operation,
            "InstanceDown",
            "10.0.0.8:9100",
            None,
            5,
        ): down,
        (
            "dev-local",
            "dev",
            operation,
            "HostHighCpu",
            "node-1:9100",
            "fp-003",
            5,
        ): short_host,
    }

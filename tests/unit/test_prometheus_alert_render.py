"""Prometheus 告警回答展示事实，不生成自动根因。"""

import datetime as dt

from xiaowei_agent.capabilities.prometheus_alert import (
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMETHEUS_ALERT_CAPABILITY_VERSION,
)
from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource, TaskStatus
from xiaowei_agent.reflection.prometheus_alert import assess_prometheus_alert
from xiaowei_agent.rendering.prometheus_alert import render_prometheus_alert

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _evidence(step: str, facts: tuple[dict[str, object], ...]) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=f"task-1:{step}",
        capability_id=PROMETHEUS_ALERT_CAPABILITY_ID,
        capability_version=PROMETHEUS_ALERT_CAPABILITY_VERSION,
        facts=facts,
        source="synthetic-recording",
        source_kind=ExternalSource.TOOL,
        captured_at=AT,
        sampled=False,
        limitations=("window: [11:30, 12:00]", "synthetic template",),
    )


ALERT = {
    "fingerprint": "fp-001",
    "state": "suppressed",
    "alert_name": "HostHighCpu",
    "instance": "node-1.example.com:9100",
    "severity": "warning",
    "starts_at": "2026-09-05T11:20:00+00:00",
    "ends_at": None,
    "silenced": True,
    "inhibited": False,
}
METRIC = {
    "metric_name": "node_cpu_percent",
    "instance": "node-1.example.com:9100",
    "point_count": 2,
    "first_value": 82.0,
    "latest_value": 91.5,
    "min_value": 82.0,
    "max_value": 91.5,
    "delta": 9.5,
    "trend": "rising",
}


def _render(evidences: tuple[EvidenceEnvelope, ...], status: TaskStatus):
    return render_prometheus_alert(
        evidences=evidences,
        verdict=assess_prometheus_alert(evidences=evidences),
        status=status,
    )


def test_success_renders_both_fact_sets_and_denies_automatic_root_cause() -> None:
    payload = _render(
        (_evidence("s1", (ALERT,)), _evidence("s2", (METRIC,))),
        TaskStatus.SUCCEEDED,
    )
    dumped = payload.model_dump_json()
    assert "suppressed" in dumped
    assert "silenced=True" in dumped
    assert "latest=91.5" in dumped
    assert "不是自动根因" in dumped
    assert "仍在触发" not in dumped


def test_multiple_alerts_request_fingerprint_without_choosing_one() -> None:
    payload = _render(
        (_evidence("s1", (ALERT, ALERT | {"fingerprint": "fp-002"})),),
        TaskStatus.INDETERMINATE,
    )
    assert payload.status is TaskStatus.INDETERMINATE
    assert any("fingerprint" in item for item in payload.next_steps)


def test_missing_metric_never_renders_success() -> None:
    evidences = (_evidence("s1", (ALERT,)), _evidence("s2", ()))
    payload = _render(evidences, TaskStatus.INDETERMINATE)
    assert payload.status is TaskStatus.INDETERMINATE
    assert "证据不足" in payload.answer


def test_rendering_is_deterministic_and_references_all_evidence() -> None:
    evidences = (_evidence("s1", (ALERT,)), _evidence("s2", (METRIC,)))
    first = _render(evidences, TaskStatus.SUCCEEDED)
    assert first == _render(evidences, TaskStatus.SUCCEEDED)
    assert first.refs == ("task-1:s1", "task-1:s2")

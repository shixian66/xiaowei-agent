"""Prometheus 告警证据的可答性真值表。"""

import datetime as dt

from xiaowei_agent.capabilities.prometheus_alert import (
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMETHEUS_ALERT_CAPABILITY_VERSION,
)
from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource, TaskStatus
from xiaowei_agent.reflection.prometheus_alert import assess_prometheus_alert
from xiaowei_agent.reflection.status import terminal_status_for

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
        limitations=("synthetic evidence only",),
    )


ALERT = {"fingerprint": "fp-001", "state": "firing"}
METRIC = {"metric_name": "node_cpu_percent", "point_count": 2}


def test_one_alert_and_metric_series_is_answerable() -> None:
    verdict = assess_prometheus_alert(
        evidences=(_evidence("s1", (ALERT,)), _evidence("s2", (METRIC,)))
    )
    assert verdict.sufficient is True
    assert terminal_status_for(verdict) is TaskStatus.SUCCEEDED


def test_one_alert_without_metric_is_indeterminate() -> None:
    verdict = assess_prometheus_alert(
        evidences=(_evidence("s1", (ALERT,)), _evidence("s2", ()))
    )
    assert verdict.sufficient is False
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_no_alert_cannot_bind_existing_metric_to_a_current_alert() -> None:
    verdict = assess_prometheus_alert(
        evidences=(_evidence("s1", ()), _evidence("s2", (METRIC,)))
    )
    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True


def test_multiple_alerts_require_a_fingerprint() -> None:
    verdict = assess_prometheus_alert(
        evidences=(_evidence("s1", (ALERT, {"fingerprint": "fp-002"})),)
    )
    assert verdict.sufficient is False
    assert verdict.needs_user_input is True
    assert any(item.key == "fingerprint" for item in verdict.missing)


def test_failed_or_missing_steps_are_indeterminate() -> None:
    for evidences in ((), (_evidence("s1", (ALERT,)),)):
        verdict = assess_prometheus_alert(evidences=evidences)
        assert verdict.sufficient is False
        assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE

"""Prometheus 告警参数只接受固定模板所需的闭集输入。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams

_END = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _params(**overrides: object) -> PrometheusAlertParams:
    values: dict[str, object] = {
        "alert_name": "HostHighCpu",
        "instance": "NODE-1.EXAMPLE.COM.:9100",
        "window_start": _END - dt.timedelta(minutes=30),
        "window_end": _END,
    }
    return PrometheusAlertParams(**(values | overrides))


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("NODE-1.EXAMPLE.COM.:9100", "node-1.example.com:9100"),
        ("10.0.0.008:9100", None),
        ("10.0.0.8:9100", "10.0.0.8:9100"),
        ("[2001:0DB8::1]:9100", "[2001:db8::1]:9100"),
        ("[2001:db8::1]", "[2001:db8::1]"),
    ],
)
def test_instance_canonicalization(raw: str, canonical: str | None) -> None:
    if canonical is None:
        with pytest.raises(ValidationError):
            _params(instance=raw)
    else:
        assert _params(instance=raw).instance == canonical


@pytest.mark.parametrize(
    "instance",
    [
        "node-*",
        "node{job=x}",
        'node"} or up{instance="x',
        "node\\name",
        "node\nname",
        "2001:db8::1",
        "node:0",
        "node:65536",
        "-node:9100",
    ],
)
def test_hostile_or_ambiguous_instances_are_rejected(instance: str) -> None:
    with pytest.raises(ValidationError):
        _params(instance=instance)


@pytest.mark.parametrize(
    "overrides",
    [
        {"alert_name": "UnknownAlert"},
        {"fingerprint": "bad value"},
        {"window_start": _END},
        {"window_start": _END - dt.timedelta(minutes=361)},
        {"window_start": dt.datetime(2026, 9, 5, 11, 30)},
        {"step_seconds": 30},
        {"max_series": 6},
        {"max_points_per_series": 362},
    ],
)
def test_closed_values_and_budgets_are_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _params(**overrides)


def test_typed_arguments_round_trip_preserves_the_canonical_form() -> None:
    original = _params(fingerprint="fp-001")
    restored = PrometheusAlertParams.from_typed_arguments(
        original.to_typed_arguments()
        | {
            "promql": 'up{instance="node-1.example.com:9100"}',
            "promql_template_id": "prometheus.alert.instance_up.v1",
        }
    )
    assert restored == original


@pytest.mark.parametrize("key", ["gateway", "operation", "template_id", "raw_promql"])
def test_unknown_typed_arguments_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        PrometheusAlertParams.from_typed_arguments(
            _params().to_typed_arguments() | {key: "polluted"}
        )

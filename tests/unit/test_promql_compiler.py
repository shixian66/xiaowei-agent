"""PromQL 只能由两个注册模板确定性编译。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import PromqlSurface
from xiaowei_agent.planning.prometheus.compiler import compile_promql
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.templates import (
    CPU_PERCENT_V1,
    INSTANCE_UP_V1,
    escape_promql_label_value,
    template_for_alert,
)

_SURFACE = PromqlSurface(
    surface_id="promql.prometheus.alert.evidence.v1",
    allowed_template_ids=(CPU_PERCENT_V1, INSTANCE_UP_V1),
    max_window_minutes=360,
    max_series=5,
    max_points_per_series=361,
)
_END = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _params(alert_name: str = "HostHighCpu") -> PrometheusAlertParams:
    return PrometheusAlertParams(
        alert_name=alert_name,
        instance="node-1.example.com:9100",
        window_start=_END - dt.timedelta(minutes=30),
        window_end=_END,
    )


def test_two_templates_have_exact_golden_output() -> None:
    assert compile_promql(
        template_id=CPU_PERCENT_V1, params=_params(), surface=_SURFACE
    ) == (
        '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
        'instance="node-1.example.com:9100"}[5m])))'
    )
    assert compile_promql(
        template_id=INSTANCE_UP_V1,
        params=_params("InstanceDown"),
        surface=_SURFACE,
    ) == 'up{instance="node-1.example.com:9100"}'


def test_repeated_compilation_is_byte_identical() -> None:
    params = _params()
    assert compile_promql(
        template_id=CPU_PERCENT_V1, params=params, surface=_SURFACE
    ) == compile_promql(template_id=CPU_PERCENT_V1, params=params, surface=_SURFACE)


def test_alert_to_template_mapping_is_closed() -> None:
    assert template_for_alert("HostHighCpu") == CPU_PERCENT_V1
    assert template_for_alert("InstanceDown") == INSTANCE_UP_V1
    with pytest.raises(ValueError):
        template_for_alert("UnknownAlert")


@pytest.mark.parametrize("template_id", ["unknown", INSTANCE_UP_V1])
def test_template_must_be_registered_and_match_the_alert(template_id: str) -> None:
    with pytest.raises(ValueError):
        compile_promql(template_id=template_id, params=_params(), surface=_SURFACE)


def test_surface_budgets_are_enforced_by_the_compiler() -> None:
    narrow = _SURFACE.model_copy(update={"max_points_per_series": 10})
    with pytest.raises(ValueError):
        compile_promql(template_id=CPU_PERCENT_V1, params=_params(), surface=narrow)


def test_label_escape_has_one_deterministic_definition() -> None:
    assert escape_promql_label_value('a"\\\n{}') == 'a\\"\\\\\\n{}'


@pytest.mark.parametrize(
    "update",
    [
        {"allowed_template_ids": ()},
        {"allowed_template_ids": (CPU_PERCENT_V1, CPU_PERCENT_V1)},
        {"max_window_minutes": 0},
        {"max_series": 0},
        {"max_points_per_series": 0},
    ],
)
def test_surface_rejects_empty_duplicate_or_nonpositive_declarations(
    update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _SURFACE.model_copy(update=update)

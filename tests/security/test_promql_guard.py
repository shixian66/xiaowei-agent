"""PromQL 准入只接受已注册模板的逐字节重编译结果。"""

import datetime as dt

import pytest

from xiaowei_agent.contracts import PromqlGuardRejection, PromqlSurface
from xiaowei_agent.governance.promqlguard import PromqlGuardError, verify_promql
from xiaowei_agent.planning.prometheus.compiler import compile_promql
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.templates import CPU_PERCENT_V1, INSTANCE_UP_V1

pytestmark = pytest.mark.security

_SURFACE = PromqlSurface(
    surface_id="promql.prometheus.alert.evidence.v1",
    allowed_template_ids=(CPU_PERCENT_V1, INSTANCE_UP_V1),
    max_window_minutes=360,
    max_series=5,
    max_points_per_series=361,
)
_PARAMS = PrometheusAlertParams(
    alert_name="HostHighCpu",
    instance="node-1.example.com:9100",
    window_start=dt.datetime(2026, 9, 5, 11, 30, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
)


def _verify(
    *,
    promql: str | None = None,
    template_id: str = CPU_PERCENT_V1,
    arguments: dict[str, object] | None = None,
    surface: PromqlSurface = _SURFACE,
) -> None:
    query = (
        compile_promql(template_id=template_id, params=_PARAMS, surface=surface)
        if promql is None
        else promql
    )
    verify_promql(
        promql=query,
        template_id=template_id,
        typed_arguments=(
            _PARAMS.to_typed_arguments() if arguments is None else arguments
        ),
        surface=surface,
    )


def test_registered_template_passes() -> None:
    _verify()


def test_unknown_template_is_rejected() -> None:
    with pytest.raises(PromqlGuardError) as caught:
        _verify(template_id="unknown", promql="up")
    assert caught.value.rejection is PromqlGuardRejection.UNKNOWN_TEMPLATE


def test_tampered_query_is_rejected() -> None:
    valid = compile_promql(
        template_id=CPU_PERCENT_V1, params=_PARAMS, surface=_SURFACE
    )
    with pytest.raises(PromqlGuardError) as caught:
        _verify(promql=valid + " or vector(1)")
    assert caught.value.rejection is PromqlGuardRejection.RECOMPILE_MISMATCH


@pytest.mark.parametrize(
    "arguments",
    [
        _PARAMS.to_typed_arguments() | {"instance": "node-2:9100"},
        _PARAMS.to_typed_arguments() | {"window_end": "invalid"},
        _PARAMS.to_typed_arguments() | {"raw_promql": "vector(1)"},
        _PARAMS.to_typed_arguments() | {"gateway": "evil"},
    ],
)
def test_tampered_or_polluted_arguments_are_rejected(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(PromqlGuardError):
        _verify(arguments=arguments)


def test_surface_without_the_template_is_rejected() -> None:
    hostile = _SURFACE.model_copy(update={"allowed_template_ids": (INSTANCE_UP_V1,)})
    with pytest.raises(PromqlGuardError) as caught:
        _verify(surface=hostile, promql="up")
    assert caught.value.rejection is PromqlGuardRejection.UNKNOWN_TEMPLATE


def test_rejection_message_does_not_echo_promql_or_arguments() -> None:
    canary = "password=" + "synthetic-promql-canary"
    with pytest.raises(PromqlGuardError) as caught:
        _verify(
            promql=canary,
            arguments=_PARAMS.to_typed_arguments() | {"instance": canary},
        )
    assert canary not in str(caught.value)
    assert caught.value.__cause__ is None

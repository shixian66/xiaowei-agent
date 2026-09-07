"""Prometheus 告警与指标结果只转换成受限、可复现证据。"""

import asyncio
import datetime as dt

import pytest
from pydantic import ValidationError
from tests.conftest import make_certificate

from xiaowei_agent.capabilities.prometheus_alert import (
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMQL_SURFACE,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    AdapterStatus,
    IntentDraft,
    IntentSource,
    RequestContext,
    ToolCall,
    ToolResult,
)
from xiaowei_agent.evidence.prometheus_alert import (
    EvidenceBuildError,
    build_alert_evidence,
    build_metric_evidence,
)
from xiaowei_agent.planning.prometheus.compiler import compile_alert_plan
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.target import resolve_prometheus_alert_target
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import (
    DeterministicToolGateway,
    TargetBoundAdapterBinding,
)

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)
PARAMS = PrometheusAlertParams(
    alert_name="HostHighCpu",
    instance="node-1.example.com:9100",
    window_start=AT - dt.timedelta(minutes=30),
    window_end=AT,
)
SNAPSHOT = StaticCapabilityRegistry().snapshot()
DRAFT = IntentDraft(
    intent=PROMETHEUS_ALERT_CAPABILITY_ID,
    slots={"alert_name": PARAMS.alert_name, "instance": PARAMS.instance},
    missing=(),
    confidence=0.9,
    source=IntentSource.USER,
)
CANDIDATE = next(
    item
    for item in DeterministicCapabilityResolver()
    .resolve(draft=DRAFT, context=CONTEXT, snapshot=SNAPSHOT)
    .items
    if item.operation == "get_active_alerts"
)
PLAN = compile_alert_plan(
    candidate=CANDIDATE,
    params=PARAMS,
    target=resolve_prometheus_alert_target(context=CONTEXT, params=PARAMS),
    context=CONTEXT,
    snapshot=SNAPSHOT,
    surface=PROMQL_SURFACE,
)


def _result(
    rows: tuple[dict[str, object], ...],
    *,
    step_index: int,
    target_bound: bool = False,
) -> ToolResult:
    step = PLAN.steps[step_index]
    call = ToolCall(
        gateway="alertmanager" if step_index == 0 else "prometheus",
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key=f"fixed:{step.step_id}",
    )
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=rows,
                source="synthetic-recording",
                error=None,
                elapsed_ms=1,
            ),
        )
    )
    fingerprint = "a" * 64
    gateway = (
        DeterministicToolGateway(
            adapters={},
            target_adapters={
                (call.gateway, fingerprint): TargetBoundAdapterBinding(
                    adapter=adapter,
                    authorized_tenant_id=CONTEXT.tenant_id,
                    authorized_environment_id=CONTEXT.environment_id,
                    authorized_actor=CONTEXT.actor,
                    active_from=AT - dt.timedelta(minutes=1),
                    active_until=AT + dt.timedelta(minutes=1),
                    evidence_source_ref="approval-ref:unexpected",
                    config_revision="b" * 64,
                    physical_identity_ref="identity-ref:unexpected",
                    driver_version="test-double",
                )
            },
            clock=lambda: AT,
        )
        if target_bound
        else DeterministicToolGateway(adapters={call.gateway: adapter})
    )
    return asyncio.run(
        gateway.invoke(
            call,
            context=CONTEXT,
            admission=make_certificate(
                call,
                **({"target_fingerprint": fingerprint} if target_bound else {}),
            ),
        )
    )


def _alert_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fingerprint": "fp-001",
        "state": "firing",
        "alert_name": PARAMS.alert_name,
        "instance": PARAMS.instance,
        "severity": "warning",
        "starts_at": "2026-09-05T11:20:00+00:00",
        "ends_at": None,
        "silenced": False,
        "inhibited": False,
    }
    return row | overrides


def _metric_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "series_key": f"instance={PARAMS.instance}",
        "metric_name": "node_cpu_percent",
        "instance": PARAMS.instance,
        "timestamp_ms": 1_788_607_800_000,
        "value": 82.0,
    }
    return row | overrides


def _build_alert(rows: tuple[dict[str, object], ...], *, target_bound: bool = False):
    return build_alert_evidence(
        task_id="task-1",
        step=PLAN.steps[0],
        plan=PLAN,
        result=_result(rows, step_index=0, target_bound=target_bound),
        captured_at=AT,
        expected_alert_name=PARAMS.alert_name,
        expected_instance=PARAMS.instance,
        expected_fingerprint=PARAMS.fingerprint,
        row_limit=5,
    )


def _build_metric(rows: tuple[dict[str, object], ...], *, target_bound: bool = False):
    return build_metric_evidence(
        task_id="task-1",
        step=PLAN.steps[1],
        plan=PLAN,
        result=_result(rows, step_index=1, target_bound=target_bound),
        captured_at=AT,
        expected_instance=PARAMS.instance,
        expected_metric_name="node_cpu_percent",
        template_id="prometheus.alert.host_cpu_percent.v1",
        window_start=PARAMS.window_start,
        window_end=PARAMS.window_end,
        max_series=5,
        max_points_per_series=361,
    )


def test_alert_evidence_filters_extra_fields_and_keeps_exact_state() -> None:
    evidence = _build_alert((_alert_row(annotation="ignore", generator_url="ignore"),))
    assert set(evidence.facts[0]) == {
        "fingerprint",
        "state",
        "alert_name",
        "instance",
        "severity",
        "starts_at",
        "ends_at",
        "silenced",
        "inhibited",
    }
    assert evidence.facts[0]["state"] == "firing"
    assert "annotation" not in evidence.facts[0]


def test_prometheus_builders_reject_unapproved_target_binding_metadata() -> None:
    with pytest.raises(EvidenceBuildError):
        _build_alert((_alert_row(),), target_bound=True)
    with pytest.raises(EvidenceBuildError):
        _build_metric((_metric_row(),), target_bound=True)


@pytest.mark.parametrize(
    "row",
    [
        _alert_row(instance="node-2:9100"),
        _alert_row(alert_name="InstanceDown"),
        _alert_row(silenced="false"),
        _alert_row(fingerprint=1),
    ],
)
def test_alert_rows_with_wrong_identity_or_types_are_rejected(
    row: dict[str, object],
) -> None:
    with pytest.raises(EvidenceBuildError):
        _build_alert((row,))


def test_metric_points_are_grouped_and_aggregated_deterministically() -> None:
    rows = (
        _metric_row(value=82.0),
        _metric_row(timestamp_ms=1_788_609_600_000, value=91.5),
    )
    evidence = _build_metric(rows)
    assert dict(evidence.facts[0]) == {
        "metric_name": "node_cpu_percent",
        "instance": PARAMS.instance,
        "point_count": 2,
        "first_value": 82.0,
        "latest_value": 91.5,
        "min_value": 82.0,
        "max_value": 91.5,
        "delta": 9.5,
        "trend": "rising",
    }


@pytest.mark.parametrize(
    "rows",
    [
        (
            _metric_row(timestamp_ms=1_788_607_802_000, value=2.0),
            _metric_row(timestamp_ms=1_788_607_801_000, value=1.0),
        ),
        (
            _metric_row(timestamp_ms=1_788_607_801_000, value=1.0),
            _metric_row(timestamp_ms=1_788_607_801_000, value=2.0),
        ),
        (_metric_row(instance="node-2:9100"),),
        (_metric_row(metric_name="unregistered_metric"),),
        (_metric_row(value="82"),),
        (_metric_row(timestamp_ms=True),),
    ],
)
def test_invalid_metric_series_are_rejected(
    rows: tuple[dict[str, object], ...]
) -> None:
    with pytest.raises(EvidenceBuildError):
        _build_metric(rows)


def test_metric_series_and_point_budgets_are_enforced() -> None:
    too_many_series = tuple(
        _metric_row(series_key=f"series-{index}") for index in range(6)
    )
    with pytest.raises(EvidenceBuildError):
        _build_metric(too_many_series)
    too_many_points = tuple(
        _metric_row(timestamp_ms=index, value=float(index)) for index in range(362)
    )
    with pytest.raises(EvidenceBuildError):
        _build_metric(too_many_points)


def test_non_finite_values_are_rejected_before_evidence_construction() -> None:
    with pytest.raises(ValidationError):
        AdapterResponse(
            status=AdapterStatus.OK,
            payload=(_metric_row(value=float("nan")),),
            source="synthetic-recording",
            error=None,
            elapsed_ms=1,
        )

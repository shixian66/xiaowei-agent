"""Prometheus 告警证据能力的 target 与固定两步计划。"""

import datetime as dt

import pytest

from xiaowei_agent.capabilities.effect import derive_effect, verify_plan_effects
from xiaowei_agent.capabilities.prometheus_alert import (
    OP_GET_ACTIVE_ALERTS,
    OP_QUERY_METRIC_RANGE,
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMETHEUS_ALERT_CAPABILITY_VERSION,
    PROMQL_SURFACE,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    Candidate,
    IntentDraft,
    IntentSource,
    RequestContext,
    StepConditionKind,
    StepResultStatus,
    ToolCall,
)
from xiaowei_agent.planning import (
    compute_plan_hash,
    compute_target_fingerprint,
    compute_tool_call_hash,
)
from xiaowei_agent.planning.prometheus.compiler import compile_alert_plan
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.target import resolve_prometheus_alert_target
from xiaowei_agent.planning.prometheus.templates import CPU_PERCENT_V1

SNAPSHOT = StaticCapabilityRegistry().snapshot()
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)
END = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _params(**overrides: object) -> PrometheusAlertParams:
    values: dict[str, object] = {
        "alert_name": "HostHighCpu",
        "instance": "node-1.example.com:9100",
        "window_start": END - dt.timedelta(minutes=30),
        "window_end": END,
    }
    return PrometheusAlertParams(**(values | overrides))


def _draft(params: PrometheusAlertParams) -> IntentDraft:
    slots = {"alert_name": params.alert_name, "instance": params.instance}
    if params.fingerprint is not None:
        slots["fingerprint"] = params.fingerprint
    return IntentDraft(
        intent=PROMETHEUS_ALERT_CAPABILITY_ID,
        slots=slots,
        missing=(),
        confidence=0.9,
        source=IntentSource.USER,
    )


def _candidate() -> Candidate:
    candidates = DeterministicCapabilityResolver().resolve(
        draft=_draft(_params()), context=CONTEXT, snapshot=SNAPSHOT
    )
    return next(
        item for item in candidates.items if item.operation == OP_GET_ACTIVE_ALERTS
    )


def _plan(params: PrometheusAlertParams | None = None):
    resolved = _params() if params is None else params
    return compile_alert_plan(
        candidate=_candidate(),
        params=resolved,
        target=resolve_prometheus_alert_target(context=CONTEXT, params=resolved),
        context=CONTEXT,
        snapshot=SNAPSHOT,
        surface=PROMQL_SURFACE,
    )


def test_target_uses_canonical_alert_instance_and_context_scope() -> None:
    target = resolve_prometheus_alert_target(
        context=CONTEXT,
        params=_params(instance="NODE-1.EXAMPLE.COM.:9100"),
    )
    assert target.model_dump() == {
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "provider": "prometheus",
        "resource_kind": "alert_instance",
        "resource_ids": ("alert:HostHighCpu", "instance:node-1.example.com:9100"),
        "selector_version": "prometheus.alert.instance.v1",
    }


def test_equivalent_instance_spellings_have_the_same_target_fingerprint() -> None:
    hostname = resolve_prometheus_alert_target(
        context=CONTEXT, params=_params(instance="NODE-1.EXAMPLE.COM.:9100")
    )
    canonical = resolve_prometheus_alert_target(
        context=CONTEXT, params=_params(instance="node-1.example.com:9100")
    )
    assert compute_target_fingerprint(hostname) == compute_target_fingerprint(canonical)

    expanded = resolve_prometheus_alert_target(
        context=CONTEXT, params=_params(instance="[2001:0DB8::1]:9100")
    )
    compressed = resolve_prometheus_alert_target(
        context=CONTEXT, params=_params(instance="[2001:db8::1]:9100")
    )
    assert compute_target_fingerprint(expanded) == compute_target_fingerprint(compressed)


@pytest.mark.parametrize(
    "context",
    [
        CONTEXT.model_copy(update={"tenant_id": "other-tenant"}),
        CONTEXT.model_copy(update={"environment_id": "test"}),
    ],
)
def test_context_scope_changes_the_target_fingerprint(context: RequestContext) -> None:
    baseline = resolve_prometheus_alert_target(context=CONTEXT, params=_params())
    changed = resolve_prometheus_alert_target(context=context, params=_params())
    assert compute_target_fingerprint(baseline) != compute_target_fingerprint(changed)


def test_plan_has_the_fixed_two_step_shape_and_condition() -> None:
    plan = _plan()
    assert tuple((step.step_id, step.operation) for step in plan.steps) == (
        ("s1", OP_GET_ACTIVE_ALERTS),
        ("s2", OP_QUERY_METRIC_RANGE),
    )
    assert plan.steps[0].depends_on == ()
    assert plan.steps[1].depends_on == ("s1",)
    assert plan.steps[1].condition.kind is StepConditionKind.PRIOR_STEP_RESULT_IS
    assert plan.steps[1].condition.ref_step_id == "s1"
    assert plan.steps[1].condition.expected_result is StepResultStatus.OK


def test_step_arguments_are_exact_closed_sets() -> None:
    first, second = _plan().steps
    assert dict(first.typed_arguments) == {
        "alert_name": "HostHighCpu",
        "instance": "node-1.example.com:9100",
        "limit": 5,
    }
    assert set(second.typed_arguments) == {
        "promql",
        "promql_template_id",
        "alert_name",
        "instance",
        "window_start",
        "window_end",
        "step_seconds",
        "max_series",
        "max_points_per_series",
    }
    assert second.typed_arguments["promql_template_id"] == CPU_PERCENT_V1


def test_fingerprint_is_only_an_alertmanager_filter_but_changes_plan_hash() -> None:
    baseline = _plan()
    filtered = _plan(_params(fingerprint="fp-001"))
    assert filtered.steps[0].typed_arguments["fingerprint"] == "fp-001"
    assert "fingerprint" not in filtered.steps[1].typed_arguments
    assert compute_plan_hash(filtered) != compute_plan_hash(baseline)


def test_plan_and_tool_hashes_are_stable_for_identical_inputs() -> None:
    first, second = _plan(), _plan()
    assert first == second
    assert compute_plan_hash(first) == compute_plan_hash(second)
    first_calls = tuple(_call(first, step.step_id) for step in first.steps)
    second_calls = tuple(_call(second, step.step_id) for step in second.steps)
    assert tuple(map(compute_tool_call_hash, first_calls)) == tuple(
        map(compute_tool_call_hash, second_calls)
    )


def _call(plan, step_id: str) -> ToolCall:
    step = next(item for item in plan.steps if item.step_id == step_id)
    operation = derive_effect(
        SNAPSHOT,
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        operation=step.operation,
    )
    return ToolCall(
        gateway=operation.gateway,
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key=f"fixed:{step.step_id}",
    )


def test_plan_identity_policy_and_effects_come_from_the_snapshot() -> None:
    plan = _plan()
    assert (plan.capability_id, plan.capability_version) == (
        PROMETHEUS_ALERT_CAPABILITY_ID,
        PROMETHEUS_ALERT_CAPABILITY_VERSION,
    )
    assert plan.policy_profile == "readonly.prometheus.alert.evidence.v1"
    verify_plan_effects(SNAPSHOT, plan)


def test_non_entry_candidate_and_target_drift_are_rejected() -> None:
    metric_candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=_draft(_params()), context=CONTEXT, snapshot=SNAPSHOT)
        .items
        if item.operation == OP_QUERY_METRIC_RANGE
    )
    with pytest.raises(ValueError, match="entry operation"):
        compile_alert_plan(
            candidate=metric_candidate,
            params=_params(),
            target=resolve_prometheus_alert_target(context=CONTEXT, params=_params()),
            context=CONTEXT,
            snapshot=SNAPSHOT,
            surface=PROMQL_SURFACE,
        )
    other = resolve_prometheus_alert_target(
        context=CONTEXT.model_copy(update={"environment_id": "test"}),
        params=_params(),
    )
    with pytest.raises(ValueError, match="environment"):
        compile_alert_plan(
            candidate=_candidate(),
            params=_params(),
            target=other,
            context=CONTEXT,
            snapshot=SNAPSHOT,
            surface=PROMQL_SURFACE,
        )

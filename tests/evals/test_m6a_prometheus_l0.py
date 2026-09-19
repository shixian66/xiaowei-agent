"""M6a Prometheus L0：查询、权限、漂移与外部文本均 fail-closed。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.fakes.admission import POLICY_SNAPSHOT
from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.capability_input import SlotReady
from xiaowei_agent.application.default_capabilities import PROMETHEUS_ALERT_BINDING
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.prometheus_alert import PROMQL_SURFACE
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    AttemptIntent,
    RequestContext,
    TaskStatus,
    ToolCall,
)
from xiaowei_agent.governance.approval import NeverGrantingApprovalGate
from xiaowei_agent.governance.promqlguard import PromqlGuardError
from xiaowei_agent.governance.step_admission import admit_step
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.planning import compute_plan_hash
from xiaowei_agent.planning.prometheus.params import (
    PrometheusAlertParams,
    normalise_window,
)
from xiaowei_agent.tools.alertmanager_fake import (
    AlertmanagerRecordingAdapter,
    AlertmanagerRecordingNotFoundError,
)
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingAdapter

pytestmark = pytest.mark.security

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m6a_prometheus_l0.json").read_text(
        encoding="utf-8"
    )
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
_TEXT = "查告警 HostHighCpu 在 node-1.example.com:9100 的证据"
_SNAPSHOT = StaticCapabilityRegistry().snapshot()


def _by(carrier: str) -> list[dict[str, Any]]:
    return [case for case in _CASES if case["carrier"] == carrier]


def _prepared(text: str = _TEXT):
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=_context())
    candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=draft, context=_context(), snapshot=_SNAPSHOT)
        .items
        if item.operation == PROMETHEUS_ALERT_BINDING.entry_operation
    )
    verified = PROMETHEUS_ALERT_BINDING.input_binding.slot_verifier(
        candidate=candidate,
        draft=draft,
        context=_context(),
        as_of=_AT,
        user_text=text,
    )
    assert isinstance(verified, SlotReady)
    return PROMETHEUS_ALERT_BINDING.input_binding.planner(
        candidate=candidate,
        params=verified.params,
        context=_context(),
        snapshot=_SNAPSHOT,
    )


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def _adapters() -> tuple[AlertmanagerRecordingAdapter, PrometheusRecordingAdapter]:
    alerts, metrics = recordings_for("golden")
    return AlertmanagerRecordingAdapter(alerts), PrometheusRecordingAdapter(metrics)


def _call(step: Any) -> ToolCall:
    return ToolCall(
        gateway="prometheus",
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key="fixed:s2",
    )


def test_every_corpus_case_has_an_executing_driver() -> None:
    assert {case["carrier"] for case in _CASES} == {
        "param",
        "guard",
        "pollution",
        "drift",
        "cross_scope",
        "secret_error",
        "no_certificate",
    }
    assert len({case["id"] for case in _CASES}) == len(_CASES)


@pytest.mark.parametrize("case", _by("param"), ids=lambda case: case["id"])
def test_invalid_parameters_stop_before_gateway(case: dict[str, Any]) -> None:
    alertmanager, prometheus = _adapters()
    mutation = case["mutation"]
    with pytest.raises((ValidationError, ValueError)):
        if mutation == "window":
            normalise_window(as_of=_AT, window_minutes=361)
        else:
            instance = {
                "matcher": 'node-1:9100",job="other',
                "quote": 'node-1"',
                "backslash": "node-1\\:9100",
                "newline": "node-1\n:9100",
            }.get(mutation, "node-1.example.com:9100")
            PrometheusAlertParams(
                alert_name="HostHighCpu",
                instance=instance,
                window_start=_AT - dt.timedelta(minutes=30),
                window_end=_AT,
                max_points_per_series=362 if mutation == "points" else 361,
            )
    assert alertmanager.call_count == 0
    assert prometheus.call_count == 0


@pytest.mark.parametrize("case", _by("guard"), ids=lambda case: case["id"])
def test_tampered_query_envelopes_stop_at_admission(case: dict[str, Any]) -> None:
    alertmanager, prometheus = _adapters()
    prepared = _prepared()
    step = prepared.plan.steps[1]
    arguments = dict(step.typed_arguments)
    mutation = case["mutation"]
    if mutation == "raw_query":
        arguments["promql"] = "vector(1)"
    elif mutation == "template":
        arguments["promql_template_id"] = "unknown"
    elif mutation == "arguments":
        arguments["instance"] = "node-2:9100"
    elif mutation == "half_envelope":
        arguments.pop("promql_template_id")
    elif mutation == "dual_envelope":
        arguments.update(
            {"sql": "SELECT 1", "sql_template_id": "synthetic.read.v1"}
        )
    hostile = step.model_copy(update={"typed_arguments": arguments})
    plan = prepared.plan.model_copy(
        update={"steps": (prepared.plan.steps[0], hostile)}
    )
    with pytest.raises(PromqlGuardError):
        admit_step(
            step=hostile,
            plan=plan,
            call=_call(hostile),
            context=_context(),
            target=prepared.target,
            snapshot=_SNAPSHOT,
            policy_snapshot=POLICY_SNAPSHOT,
            profile=PROMETHEUS_ALERT_BINDING.execution.policy_profile,
            sql_surface=None,
            promql_surface=PROMQL_SURFACE,
            approval_gate=NeverGrantingApprovalGate(),
            approval=None,
            task_id="task-l0",
            now=_AT,
        )
    assert alertmanager.call_count == 0
    assert prometheus.call_count == 0


@pytest.mark.parametrize("case", _by("pollution"), ids=lambda case: case["id"])
def test_user_text_cannot_change_the_plan(case: dict[str, Any]) -> None:
    alertmanager, prometheus = _adapters()
    assert compute_plan_hash(_prepared().plan) == compute_plan_hash(
        _prepared(f"{_TEXT} {case['suffix']}").plan
    )
    assert alertmanager.call_count == 0
    assert prometheus.call_count == 0


@pytest.mark.parametrize("case", _by("drift"), ids=lambda case: case["id"])
async def test_stored_plan_drift_fails_before_both_adapters(case: dict[str, Any]) -> None:
    alerts, metrics = recordings_for("golden")
    harness = RuntimeHarness(
        None,
        adapters={
            "alertmanager": AlertmanagerRecordingAdapter(alerts),
            "prometheus": PrometheusRecordingAdapter(metrics),
        },
        as_of=_AT,
    )
    submission = harness.submission(_TEXT)
    pending = await harness.runtime.submit_task(submission=submission)
    harness.task_id = pending.task_id
    prepared = _prepared()
    first = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=pending.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert first.grant is not None
    await harness.plan_store.save(
        task_id=pending.task_id, plan=prepared.plan, target=prepared.target
    )
    current = await harness.store.get(lookup=harness.lookup)
    changed_status = await harness.store.transition(
        command=TransitionCommand(
            task_id=pending.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=first.grant.fencing_token,
        )
    )
    assert changed_status.applied
    stored = harness.state.plans[pending.task_id]
    first_step = stored.plan.steps[0]
    changed_step = first_step.model_copy(
        update={
            "typed_arguments": dict(first_step.typed_arguments)
            | {case["field"]: "tampered"}
        }
    )
    harness.state.plans[pending.task_id] = stored.model_copy(
        update={
            "plan": stored.plan.model_copy(
                update={"steps": (changed_step, *stored.plan.steps[1:])}
            )
        }
    )
    harness.clock.advance(seconds=61)
    resumed = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=pending.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-2",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert resumed.grant is not None and resumed.submission is not None

    outcome = await harness.runtime.execute_task(
        grant=resumed.grant, submission=resumed.submission
    )

    assert outcome.status is TaskStatus.FAILED
    assert harness.gateway.invocations == 0
    assert harness.adapters["alertmanager"].call_count == 0
    assert harness.adapters["prometheus"].call_count == 0


async def test_cross_scope_recording_has_no_fallback() -> None:
    alertmanager, prometheus = _adapters()
    first = _prepared().plan.steps[0]
    call = ToolCall(
        gateway="alertmanager",
        operation=first.operation,
        step_id=first.step_id,
        typed_args=first.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key="fixed:s1",
    )
    with pytest.raises(AlertmanagerRecordingNotFoundError):
        await alertmanager.execute(
            call,
            context=_context().model_copy(update={"tenant_id": "other"}),
        )
    assert alertmanager.call_count == 1
    assert prometheus.call_count == 0


async def test_secret_shaped_upstream_error_never_reaches_trusted_outputs() -> None:
    alerts, metrics = recordings_for("alert_error")
    harness = RuntimeHarness(
        None,
        adapters={
            "alertmanager": AlertmanagerRecordingAdapter(alerts),
            "prometheus": PrometheusRecordingAdapter(metrics),
        },
        as_of=_AT,
    )
    payload = await harness.handle(_TEXT)
    marker = "password" + "=" + "synthetic-upstream"
    joined = " ".join(
        (payload.model_dump_json(), *(event.model_dump_json() for event in harness.sink.events))
    )
    assert payload.status is TaskStatus.INDETERMINATE
    assert marker not in joined
    assert harness.adapters["alertmanager"].call_count == 1
    assert harness.adapters["prometheus"].call_count == 0


async def test_gateway_rejects_a_call_without_a_certificate() -> None:
    alertmanager, prometheus = _adapters()
    step = _prepared().plan.steps[0]
    call = ToolCall(
        gateway="alertmanager",
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key="fixed:s1",
    )
    gateway = DeterministicToolGateway(
        adapters={"alertmanager": alertmanager, "prometheus": prometheus}
    )
    with pytest.raises(PermissionError):
        await gateway.invoke(call, context=_context(), admission=None)  # type: ignore[arg-type]
    assert alertmanager.call_count == 0
    assert prometheus.call_count == 0

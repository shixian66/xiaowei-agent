"""Prometheus 告警证据能力的完整 Runtime 契约。"""

import datetime as dt

import pytest
from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.default_capabilities import PROMETHEUS_ALERT_BINDING
from xiaowei_agent.contracts import AttemptIntent, TaskStatus
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingAdapter

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
TEXT = "查告警 HostHighCpu 在 node-1.example.com:9100 的证据"


def _harness(scenario: str) -> RuntimeHarness:
    alerts, metrics = recordings_for(scenario)
    return RuntimeHarness(
        None,
        adapters={
            "alertmanager": AlertmanagerRecordingAdapter(alerts),
            "prometheus": PrometheusRecordingAdapter(metrics),
        },
        as_of=AT,
    )


@pytest.mark.parametrize(
    ("scenario", "status", "alert_calls", "metric_calls"),
    [
        ("golden", TaskStatus.SUCCEEDED, 1, 1),
        ("silenced", TaskStatus.SUCCEEDED, 1, 1),
        ("empty_metric", TaskStatus.INDETERMINATE, 1, 1),
        ("empty_alert", TaskStatus.INDETERMINATE, 1, 1),
        ("multiple_alerts", TaskStatus.INDETERMINATE, 1, 1),
        ("alert_timeout", TaskStatus.INDETERMINATE, 1, 0),
        ("prometheus_timeout", TaskStatus.INDETERMINATE, 1, 1),
        ("malformed", TaskStatus.INDETERMINATE, 1, 1),
        ("polluted", TaskStatus.SUCCEEDED, 1, 1),
    ],
)
async def test_runtime_truth_table_and_gateway_order(
    scenario: str,
    status: TaskStatus,
    alert_calls: int,
    metric_calls: int,
) -> None:
    harness = _harness(scenario)
    payload = await harness.handle(TEXT)

    assert payload.status is status
    assert harness.adapters["alertmanager"].call_count == alert_calls
    assert harness.adapters["prometheus"].call_count == metric_calls
    assert [call.gateway for call in harness.calls] == [
        *(["alertmanager"] if alert_calls else []),
        *(["prometheus"] if metric_calls else []),
    ]


async def test_terminal_query_uses_prometheus_renderer_and_is_pure() -> None:
    harness = _harness("golden")
    first = await harness.handle(TEXT)
    before = harness.gateway.invocations

    queried = await harness.runtime.query_task(lookup=harness.lookup)

    assert queried.render == first
    assert queried.render is not None
    assert "不是自动根因结论" in queried.render.answer
    assert harness.gateway.invocations == before


async def test_external_adapter_fields_never_reach_evidence_render_or_trace() -> None:
    harness = _harness("polluted")
    payload = await harness.handle(TEXT)
    marker = "token" + "=" + "synthetic-value"

    evidences = await harness.ledger.load(task_id=harness.task_id)
    serialised = " ".join(
        (
            payload.model_dump_json(),
            *(item.model_dump_json() for item in evidences),
            *(item.model_dump_json() for item in harness.sink.events),
        )
    )
    assert marker not in serialised
    assert "invalid.local" not in serialised


@pytest.mark.parametrize("field", ["alert_name", "instance", "limit"])
async def test_stored_plan_drift_stops_before_both_adapters(field: str) -> None:
    harness = _harness("golden")
    submission = harness.submission(TEXT)
    pending = await harness.runtime.submit_task(submission=submission)
    harness.task_id = pending.task_id
    draft = harness.runtime._interpreter.interpret(text=TEXT, context=harness.context)
    prepared = PROMETHEUS_ALERT_BINDING.planner(
        candidate=next(
            item
            for item in harness.runtime._resolver.resolve(
                draft=draft,
                context=harness.context,
                snapshot=harness.snapshot,
            ).items
            if item.operation == "get_active_alerts"
        ),
        draft=draft,
        context=harness.context,
        as_of=submission.as_of,
        snapshot=harness.snapshot,
    )
    first_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=pending.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert first_attempt.grant is not None
    await harness.plan_store.save(
        task_id=pending.task_id,
        plan=prepared.plan,
        target=prepared.target,
    )
    current = await harness.store.get(lookup=harness.lookup)
    planning = await harness.store.transition(
        command=TransitionCommand(
            task_id=pending.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=first_attempt.grant.fencing_token,
        )
    )
    assert planning.applied
    stored = harness.state.plans[pending.task_id]
    first = stored.plan.steps[0]
    value: object = 4 if field == "limit" else "tampered"
    changed = first.model_copy(
        update={"typed_arguments": dict(first.typed_arguments) | {field: value}}
    )
    harness.state.plans[pending.task_id] = stored.model_copy(
        update={
            "plan": stored.plan.model_copy(
                update={"steps": (changed, *stored.plan.steps[1:])}
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

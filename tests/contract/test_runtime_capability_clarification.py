"""Runtime saves capability-level clarification before planning or gateway."""

import datetime as dt

from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import (
    AttemptIntent,
    CapabilitySubject,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedTextValue,
    TaskStatus,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingAdapter

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _prometheus_harness() -> RuntimeHarness:
    alerts, metrics = recordings_for("golden")
    return RuntimeHarness(
        None,
        adapters={
            "alertmanager": AlertmanagerRecordingAdapter(alerts),
            "prometheus": PrometheusRecordingAdapter(metrics),
        },
        as_of=AT,
    )


async def test_capability_incomplete_saves_capability_subject_and_stops_gateway() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="capability-clarify",
    )
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None

    outcome = await harness.runtime.execute_task(
        grant=attempt.grant, submission=attempt.submission
    )

    assert outcome.status is TaskStatus.CLARIFICATION_REQUIRED
    assert harness.gateway.invocations == 0
    saved = await harness.clarification_records.load(task_id=view.task_id)
    assert saved is not None
    assert isinstance(saved.subject, CapabilitySubject)
    assert saved.subject.capability_id == "prometheus.alert.evidence"
    assert saved.subject.operation == "get_active_alerts"
    assert saved.subject.input_schema_ref == "input.prometheus.alert.v1"
    assert saved.reason_code is ClarificationReasonCode.CAPABILITY_FIELDS_MISSING
    assert saved.missing_fields == (ClarificationField.INSTANCE,)
    assert saved.confirmed_slots == saved.subject.confirmed_slots
    assert saved.confirmed_slots[0].field is ClarificationField.ALERT_NAME
    assert isinstance(saved.confirmed_slots[0].value, ConfirmedTextValue)
    assert saved.confirmed_slots[0].value.text == "HostHighCpu"

    projected = await harness.runtime.query_task(lookup=harness.lookup)
    assert projected.status is TaskStatus.CLARIFICATION_REQUIRED
    assert projected.clarification is not None
    assert projected.clarification.reason_code is saved.reason_code
    assert projected.clarification.missing_fields == saved.missing_fields


async def test_capability_child_can_answer_only_the_missing_slot() -> None:
    harness = _prometheus_harness()
    parent_submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="capability-parent",
    )
    parent = await harness.runtime.submit_task(submission=parent_submission)
    parent_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=parent.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=parent_submission.context.trace_id,
        )
    )
    assert parent_attempt.grant is not None and parent_attempt.submission is not None

    parent_outcome = await harness.runtime.execute_task(
        grant=parent_attempt.grant,
        submission=parent_attempt.submission,
    )
    assert parent_outcome.status is TaskStatus.CLARIFICATION_REQUIRED

    child_submission = harness.submission(
        "node-1.example.com:9100",
        idempotency_key="capability-child",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id
    child_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=child.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-2",
            ttl_seconds=60,
            trace_id=child_submission.context.trace_id,
        )
    )
    assert child_attempt.grant is not None

    child_outcome = await harness.runtime.execute_task(
        grant=child_attempt.grant,
        submission=child_submission,
    )

    assert child_outcome.status is TaskStatus.SUCCEEDED
    assert harness.adapters["alertmanager"].call_count == 1
    assert harness.adapters["prometheus"].call_count == 1

"""Runtime saves capability-level clarification before planning or gateway."""

import datetime as dt

import pytest
from tests.fakes.asset_recordings import recording_for as asset_recording_for
from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import (
    AttemptIntent,
    CapabilitySubject,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedTextValue,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionRejectionReasonCode,
    InteractionSource,
    ModelUsage,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingAdapter
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


def _asset_harness() -> RuntimeHarness:
    return RuntimeHarness(
        None,
        adapters={
            "asset_inventory": AssetInventoryRecordingAdapter(
                asset_recording_for("golden"), operation="lookup_asset"
            )
        },
        as_of=AT,
    )


class _ConversationPort:
    calls: int = 0

    async def classify(self, request: object) -> InteractionModelResult:
        del request
        self.calls += 1
        return InteractionModelResult(
            draft=InteractionDraft(
                proposed_kind=InteractionKind.CONVERSATION,
                capability_draft=None,
                confidence=0.8,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(input_tokens=4, output_tokens=3),
        )


async def _execute_submission(
    harness: RuntimeHarness, submission: TaskSubmission, *, owner: str
) -> object:
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=harness.task_id,
            intent=AttemptIntent.DISPATCH,
            owner=owner,
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None
    return await harness.runtime.execute_task(
        grant=attempt.grant,
        submission=submission,
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
    assert "instance" in projected.clarification.prompt


@pytest.mark.parametrize(
    ("child_text", "expected_status"),
    [
        ("node-1.example.com:9100", TaskStatus.SUCCEEDED),
        ("instance=node-1.example.com:9100", TaskStatus.SUCCEEDED),
        ("实例是 node-1.example.com:9100", TaskStatus.REJECTED),
    ],
)
async def test_prometheus_capability_child_reply_shapes_are_stable(
    child_text: str, expected_status: TaskStatus
) -> None:
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
        child_text,
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

    assert child_outcome.status is expected_status
    expected_calls = 1 if expected_status is TaskStatus.SUCCEEDED else 0
    assert harness.adapters["alertmanager"].call_count == expected_calls
    assert harness.adapters["prometheus"].call_count == expected_calls


async def test_conversation_child_reply_rejects_instead_of_consuming_parent_as_success() -> None:
    harness = _prometheus_harness()
    parent_submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="conversation-parent",
    )
    parent = await harness.runtime.submit_task(submission=parent_submission)
    harness.task_id = parent.task_id
    parent_outcome = await _execute_submission(
        harness, parent_submission, owner="worker-conversation-parent"
    )
    assert parent_outcome.status is TaskStatus.CLARIFICATION_REQUIRED

    port = _ConversationPort()
    harness.runtime._interaction_classifier = port
    child_submission = harness.submission(
        "你能做什么？",
        idempotency_key="conversation-child",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id

    child_outcome = await _execute_submission(
        harness, child_submission, owner="worker-conversation-child"
    )
    projected = await harness.runtime.query_task(lookup=harness.lookup)

    assert child_outcome.status is TaskStatus.REJECTED
    assert (
        child_outcome.terminal_reason
        == InteractionRejectionReasonCode.CLARIFICATION_SUBJECT_INCOMPATIBLE.value
    )
    assert projected.render is not None
    assert projected.render.status is TaskStatus.REJECTED
    assert "普通对话通道" not in projected.render.answer
    assert port.calls == 1
    assert harness.gateway.invocations == 0
    assert harness.adapters["alertmanager"].call_count == 0
    assert harness.adapters["prometheus"].call_count == 0


async def test_runtime_passes_parent_confirmed_slots_to_runner_disclosure() -> None:
    harness = _prometheus_harness()
    captured: list[object] = []
    inner_runner = harness.runtime._runner

    class CapturingRunner:
        async def start(self, grant: object, **kwargs: object) -> object:
            captured.append(kwargs.get("parent_confirmed_slots"))
            return await inner_runner.start(grant, **kwargs)  # type: ignore[arg-type]

        async def resume(self, grant: object, **kwargs: object) -> object:
            captured.append(kwargs.get("parent_confirmed_slots"))
            return await inner_runner.resume(grant, **kwargs)  # type: ignore[arg-type]

    harness.runtime._runner = CapturingRunner()  # type: ignore[assignment]
    parent_submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="parent-disclosure-slots",
    )
    parent = await harness.runtime.submit_task(submission=parent_submission)
    parent_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=parent.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-parent-disclosure",
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
    saved = await harness.clarification_records.load(task_id=parent.task_id)
    assert saved is not None

    child_submission = harness.submission(
        "node-1.example.com:9100",
        idempotency_key="child-disclosure-slots",
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
            owner="worker-child-disclosure",
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
    assert captured == [saved.confirmed_slots]


@pytest.mark.parametrize(
    ("child_text", "expected_status"),
    [
        ("node-1.example.com", TaskStatus.SUCCEEDED),
        ("hostname=node-1.example.com", TaskStatus.SUCCEEDED),
        ("主机是 node-1.example.com", TaskStatus.REJECTED),
    ],
)
async def test_asset_capability_child_reply_shapes_are_stable(
    child_text: str, expected_status: TaskStatus
) -> None:
    harness = _asset_harness()
    parent_submission = harness.submission(
        "查资产",
        idempotency_key="asset-parent",
    )
    parent = await harness.runtime.submit_task(submission=parent_submission)
    harness.task_id = parent.task_id
    parent_outcome = await _execute_submission(
        harness, parent_submission, owner="worker-asset-parent"
    )
    assert parent_outcome.status is TaskStatus.CLARIFICATION_REQUIRED

    child_submission = harness.submission(
        child_text,
        idempotency_key="asset-child",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id

    child_outcome = await _execute_submission(
        harness, child_submission, owner="worker-asset-child"
    )

    assert child_outcome.status is expected_status
    expected_calls = 1 if expected_status is TaskStatus.SUCCEEDED else 0
    assert harness.adapters["asset_inventory"].call_count == expected_calls

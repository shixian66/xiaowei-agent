"""Clarification parent subjects cannot retarget capability input binding."""

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import (
    AttemptIntent,
    CapabilitySubject,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedSlot,
    ConfirmedTextValue,
    TaskStatus,
)
from xiaowei_agent.persistence.clarification_records import ClarificationRecordCandidate
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand

pytestmark = pytest.mark.security


async def test_child_rejects_incompatible_parent_capability_subject() -> None:
    harness = RuntimeHarness(GOLDEN)
    parent_submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="parent-incompatible",
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
    assert parent_attempt.grant is not None
    slot = ConfirmedSlot(
        field=ClarificationField.ALERT_NAME,
        value=ConfirmedTextValue(kind="text", text="HostHighCpu"),
    )
    await harness.clarification_records.save(
        grant=parent_attempt.grant,
        candidate=ClarificationRecordCandidate(
            subject=CapabilitySubject(
                kind="capability",
                capability_id="asset.inventory.lookup",
                capability_version="1.0.0",
                operation="lookup_asset",
                input_schema_ref="input.asset.lookup.v1",
                confirmed_slots=(slot,),
            ),
            reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
            missing_fields=(ClarificationField.INSTANCE,),
            confirmed_slots=(slot,),
        ),
    )
    transitioned = await harness.store.transition(
        command=TransitionCommand(
            task_id=parent.task_id,
            expected_version=parent_attempt.winner.version,
            to_status=TaskStatus.CLARIFICATION_REQUIRED,
            fencing_token=parent_attempt.grant.fencing_token,
        )
    )
    assert transitioned.applied

    child_submission = harness.submission(
        "查告警 HostHighCpu 在 node-1.example.com:9100 的证据",
        idempotency_key="child-incompatible",
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

    outcome = await harness.runtime.execute_task(
        grant=child_attempt.grant,
        submission=child_submission,
    )

    assert outcome.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0

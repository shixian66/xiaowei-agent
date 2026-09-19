"""CLARIFICATION_REQUIRED 终态必须从 ClarificationRecord 投影。"""

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import (
    ClarificationField,
    ClarificationPayload,
    ClarificationReasonCode,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.persistence.store import TransitionCommand


async def test_task_view_projects_clarification_from_record_only() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("我想查一下，但没说清楚")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=harness.attempt_command(view.task_id)
    )
    assert attempt.grant is not None

    saved = await harness.clarification_records.save(
        grant=attempt.grant,
        candidate=harness.route_clarification_candidate(),
    )
    result = await harness.store.transition(
        command=TransitionCommand(
            task_id=view.task_id,
            expected_version=attempt.winner.version,
            to_status=TaskStatus.CLARIFICATION_REQUIRED,
            fencing_token=attempt.grant.fencing_token,
        )
    )
    assert result.applied

    projected = await harness.runtime.query_task(
        lookup=TaskLookup(
            task_id=view.task_id,
            tenant_id=submission.context.tenant_id,
            environment_id=submission.context.environment_id,
        )
    )

    assert projected.status is TaskStatus.CLARIFICATION_REQUIRED
    assert projected.render is None
    assert projected.clarification == ClarificationPayload(
        reason_code=saved.reason_code,
        missing_fields=saved.missing_fields,
        confirmed_slots=saved.confirmed_slots,
        prompt="我还需要确认这次请求的类型，请补充更明确的运维目标。",
    )


async def test_clarification_terminal_with_missing_record_is_integrity_error() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("我想查一下，但没说清楚")
    view = await harness.runtime.submit_task(submission=submission)
    attempt = await harness.store.begin_task_attempt(
        command=harness.attempt_command(view.task_id)
    )
    assert attempt.grant is not None
    result = await harness.store.transition(
        command=TransitionCommand(
            task_id=view.task_id,
            expected_version=attempt.winner.version,
            to_status=TaskStatus.CLARIFICATION_REQUIRED,
            fencing_token=attempt.grant.fencing_token,
        )
    )
    assert result.applied

    with pytest.raises(RuntimeError, match=r"clarification\.integrity_error"):
        await harness.runtime.query_task(
            lookup=TaskLookup(
                task_id=view.task_id,
                tenant_id=submission.context.tenant_id,
                environment_id=submission.context.environment_id,
            )
        )


def test_clarification_payload_shape_still_excludes_render() -> None:
    payload = ClarificationPayload(
        reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
        missing_fields=(ClarificationField.TIME_RANGE,),
        prompt="请补充时间范围。",
    )

    assert payload.confirmed_slots == ()

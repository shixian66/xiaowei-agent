"""澄清终态不能从 terminal_reason、原文或模型输出临时拼投影。"""

from unittest.mock import patch

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.task_view_runtime import ClarificationIntegrityError
from xiaowei_agent.contracts import (
    AttemptIntent,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelUsage,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand

pytestmark = pytest.mark.security


class _UnknownInteractionPort:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, request: object) -> InteractionModelResult:
        self.calls += 1
        return InteractionModelResult(
            draft=InteractionDraft(
                proposed_kind=InteractionKind.UNKNOWN,
                capability_draft=None,
                confidence=0.3,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(input_tokens=4, output_tokens=2),
        )


async def test_route_clarification_persists_record_before_terminal_state() -> None:
    harness = RuntimeHarness(
        GOLDEN,
        interaction_classifier=_UnknownInteractionPort(),
    )
    submission = harness.submission("你自己看着办")
    view = await harness.runtime.submit_task(submission=submission)
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
        grant=attempt.grant,
        submission=attempt.submission,
    )

    assert outcome.status is TaskStatus.CLARIFICATION_REQUIRED
    assert harness.calls == []
    record = await harness.clarification_records.load(task_id=view.task_id)
    assert record is not None
    assert record.reason_code.value == "interaction.kind_ambiguous"
    projected = await harness.runtime.query_task(
        lookup=TaskLookup(
            task_id=view.task_id,
            tenant_id=submission.context.tenant_id,
            environment_id=submission.context.environment_id,
        )
    )
    assert projected.clarification is not None
    assert projected.render is None


async def test_missing_record_for_clarification_terminal_fails_closed() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("你自己看着办")
    view = await harness.runtime.submit_task(submission=submission)
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None
    result = await harness.store.transition(
        command=harness.transition_to_clarification(view.task_id, attempt)
    )
    assert result.applied

    with pytest.raises(ClarificationIntegrityError):
        await harness.runtime.query_task(
            lookup=TaskLookup(
                task_id=view.task_id,
                tenant_id=submission.context.tenant_id,
                environment_id=submission.context.environment_id,
            )
        )


async def test_crash_after_clarification_record_save_recovers_without_replay() -> None:
    classifier = _UnknownInteractionPort()
    harness = RuntimeHarness(
        GOLDEN,
        interaction_classifier=classifier,
    )
    submission = harness.submission("你自己看着办")
    view = await harness.runtime.submit_task(submission=submission)
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

    with patch.object(
        harness.runtime,
        "_finalize",
        side_effect=RuntimeError("simulated crash after clarification save"),
    ):
        with pytest.raises(RuntimeError, match="simulated crash"):
            await harness.runtime.execute_task(
                grant=attempt.grant,
                submission=attempt.submission,
            )

    stored_after_crash = await harness.clarification_records.load(task_id=view.task_id)
    assert stored_after_crash is not None
    after_crash = await harness.store.get(
        lookup=TaskLookup(
            task_id=view.task_id,
            tenant_id=submission.context.tenant_id,
            environment_id=submission.context.environment_id,
        )
    )
    assert after_crash.status is TaskStatus.CREATED
    assert classifier.calls == 1

    harness.clock.advance(seconds=61)
    recovered = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-2",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert recovered.grant is not None and recovered.submission is not None

    outcome = await harness.runtime.execute_task(
        grant=recovered.grant,
        submission=recovered.submission,
    )

    assert outcome.status is TaskStatus.CLARIFICATION_REQUIRED
    assert classifier.calls == 1
    assert harness.calls == []
    projected = await harness.runtime.query_task(
        lookup=TaskLookup(
            task_id=view.task_id,
            tenant_id=submission.context.tenant_id,
            environment_id=submission.context.environment_id,
        )
    )
    assert projected.clarification is not None
    assert projected.render is None

"""Execution disclosure is a durable barrier before admission/gateway."""

from dataclasses import replace

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runner import RunnerHarness

from xiaowei_agent.contracts import (
    AttemptIntent,
    ClarificationField,
    ConfirmedSlot,
    PipelineStage,
    StageOutcome,
    TaskStatus,
)
from xiaowei_agent.observability.sink import Delivery
from xiaowei_agent.persistence.store import TaskAttemptCommand, TaskAttemptGrant, TransitionCommand
from xiaowei_agent.planning.disclosure import DisclosureProjectionError
from xiaowei_agent.planning.slot_verification import confirmed_text_value

pytestmark = pytest.mark.security


async def _begin(harness: RunnerHarness, intent: AttemptIntent) -> TaskAttemptGrant:
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=harness.task_id,
            intent=intent,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert attempt.grant is not None
    return attempt.grant


async def _prepare_resume_from_planning(harness: RunnerHarness) -> TaskAttemptGrant:
    await harness.ensure_task()
    grant = await _begin(harness, AttemptIntent.DISPATCH)
    await harness.plan_store.save(
        task_id=harness.task_id,
        plan=harness.plan,
        target=harness.target,
    )
    record = await harness.store.get(lookup=harness.lookup)
    result = await harness.store.transition(
        command=TransitionCommand(
            task_id=harness.task_id,
            expected_version=record.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=grant.fencing_token,
        )
    )
    assert result.applied
    harness.clock.advance(seconds=61)
    return await _begin(harness, AttemptIntent.DISPATCH)


async def test_start_persists_disclosure_before_admission_and_gateway() -> None:
    harness = RunnerHarness(GOLDEN)

    await harness.start()

    stages = [event.stage for event in harness.sink.events]
    assert PipelineStage.DISCLOSURE in stages
    assert stages.index(PipelineStage.DISCLOSURE) < stages.index(
        PipelineStage.ADMISSION
    )
    assert stages.index(PipelineStage.DISCLOSURE) < stages.index(PipelineStage.GATEWAY)
    disclosure_index = stages.index(PipelineStage.DISCLOSURE)
    disclosure = harness.sink.events[disclosure_index]
    assert disclosure.outcome is StageOutcome.OK
    assert disclosure.step_id is None
    assert harness.sink.deliveries[disclosure_index] is Delivery.LOG_AND_DURABLE


async def test_resume_persists_disclosure_before_admission_and_gateway() -> None:
    harness = RunnerHarness(GOLDEN)
    grant = await _prepare_resume_from_planning(harness)

    await harness.runner.resume(
        grant,
        plan=harness.plan,
        context=harness.context,
        target=harness.target,
    )

    stages = [event.stage for event in harness.sink.events]
    assert PipelineStage.DISCLOSURE in stages
    assert stages.index(PipelineStage.DISCLOSURE) < stages.index(
        PipelineStage.ADMISSION
    )
    assert stages.index(PipelineStage.DISCLOSURE) < stages.index(PipelineStage.GATEWAY)


async def test_projection_failure_stops_before_admission_and_gateway() -> None:
    harness = RunnerHarness(GOLDEN)
    inner = harness.runner._bindings

    def _raise(**_: object) -> tuple[object, ...]:
        raise RuntimeError("projection should not leak this message")

    class BrokenDisclosureBindings:
        def execution_for(self, *, plan: object) -> object:
            execution = inner.execution_for(plan=plan)  # type: ignore[arg-type]
            return replace(
                execution,
                disclosure=replace(
                    execution.disclosure,
                    confirmed_slot_projector=_raise,
                ),
            )

    harness.runner._bindings = BrokenDisclosureBindings()  # type: ignore[assignment]

    with pytest.raises(DisclosureProjectionError):
        await harness.start()

    stages = [event.stage for event in harness.sink.events]
    assert stages[-1] is PipelineStage.DISCLOSURE
    assert harness.sink.events[-1].outcome is StageOutcome.FAILED
    assert PipelineStage.ADMISSION not in stages
    assert PipelineStage.GATEWAY not in stages
    assert harness.gateway.invocations == 0


async def test_parent_snapshot_is_passed_to_disclosure_projector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xiaowei_agent.runners.deterministic as deterministic_module

    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    grant = await _begin(harness, AttemptIntent.DISPATCH)
    parent = (
        ConfirmedSlot(
            field=ClarificationField.DATABASE,
            value=confirmed_text_value("analytics"),
        ),
    )
    seen: list[tuple[ConfirmedSlot, ...] | None] = []
    original = deterministic_module.project_execution_disclosure

    def _capture(**kwargs: object) -> object:
        seen.append(kwargs.get("parent_confirmed_slots"))  # type: ignore[arg-type]
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        deterministic_module,
        "project_execution_disclosure",
        _capture,
    )

    await harness.runner.start(
        grant,
        plan=harness.plan,
        target=harness.target,
        context=harness.context,
        parent_confirmed_slots=parent,
    )

    assert seen == [parent]

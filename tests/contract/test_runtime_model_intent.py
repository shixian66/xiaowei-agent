"""Durable execute path 的 accepted-intent 恢复边界。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.conftest import make_submission
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.model_intent import load_or_accept_intent
from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.contracts import (
    AttemptIntent,
    IntentDraft,
    IntentModelResult,
    IntentSource,
    ModelErrorCode,
    ModelInvocationProfile,
    ModelUsage,
    PipelineStage,
    RequestEnvelope,
    StageOutcome,
    TaskStatus,
)
from xiaowei_agent.persistence import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.model_artifacts import ModelArtifactConflictError
from xiaowei_agent.persistence.plans import PlanNotFoundError
from xiaowei_agent.persistence.store import TaskAttemptCommand


class _IntentPort:
    def __init__(self, result: IntentModelResult | ModelErrorCode) -> None:
        self.result = result
        self.calls = 0

    async def generate_intent(self, request: Any) -> IntentModelResult:
        del request
        self.calls += 1
        if isinstance(self.result, ModelErrorCode):
            raise ModelPortError(self.result)
        return self.result


class _BlockingIntentPort:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate_intent(self, request: Any) -> IntentModelResult:
        del request
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")


def _result() -> IntentModelResult:
    return IntentModelResult(
        draft=IntentDraft(
            intent="starrocks.slow_query.diagnose",
            slots={"environment_id": "dev", "window_minutes": "30"},
            missing=(),
            confidence=0.8,
            source=IntentSource.MODEL,
        ),
        usage=ModelUsage(input_tokens=9, output_tokens=4),
    )


async def _grant(store: Any, context: Any) -> tuple[Any, Any]:
    submission = make_submission(context)
    record = await store.create_task(submission=submission)
    attempted = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=record.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=context.trace_id,
        )
    )
    assert attempted.grant is not None
    return attempted.grant, submission


@pytest.mark.asyncio
async def test_saved_intent_is_reused_without_a_second_provider_call(
    store: Any, model_artifact_store: Any, context: Any
) -> None:
    grant, submission = await _grant(store, context)
    port = _IntentPort(_result())
    arguments = {
        "grant": grant,
        "envelope": submission.envelope,
        "context": context,
        "history": (),
        "interpreter": RuleBasedIntentInterpreter(),
        "model": port,
        "profile": ModelInvocationProfile(),
        "artifacts": model_artifact_store,
    }

    first = await load_or_accept_intent(**arguments)
    second = await load_or_accept_intent(**arguments)

    assert first.artifact == second.artifact
    assert first.observation is not None
    assert second.observation is None
    assert port.calls == 1


@pytest.mark.asyncio
async def test_stored_intent_is_not_reused_for_different_input(
    store: Any, model_artifact_store: Any, context: Any
) -> None:
    grant, submission = await _grant(store, context)
    port = _IntentPort(_result())
    base = {
        "grant": grant,
        "context": context,
        "history": (),
        "interpreter": RuleBasedIntentInterpreter(),
        "model": port,
        "profile": ModelInvocationProfile(),
        "artifacts": model_artifact_store,
    }
    await load_or_accept_intent(envelope=submission.envelope, **base)
    changed = RequestEnvelope(
        **{
            **submission.envelope.model_dump(mode="python"),
            "text": "另一条慢查询请求",
        }
    )

    with pytest.raises(ModelArtifactConflictError):
        await load_or_accept_intent(envelope=changed, **base)
    assert port.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        {"provider": "drifted-provider"},
        {"model": "drifted-model"},
        {"provider_origin": "https://drifted.example.invalid"},
        {"prompt_revision": "drifted-prompt"},
        {"schema_revision": "drifted-schema"},
    ],
)
async def test_stored_intent_is_not_reused_after_identity_metadata_drift(
    update: dict[str, str],
    store: Any,
    model_artifact_store: Any,
    memory_state: Any,
    context: Any,
) -> None:
    grant, submission = await _grant(store, context)
    port = _IntentPort(_result())
    arguments = {
        "grant": grant,
        "envelope": submission.envelope,
        "context": context,
        "history": (),
        "interpreter": RuleBasedIntentInterpreter(),
        "model": port,
        "profile": ModelInvocationProfile(),
        "artifacts": model_artifact_store,
    }
    accepted = await load_or_accept_intent(**arguments)
    memory_state.accepted_intents[grant.task_id] = accepted.artifact.model_copy(
        update=update
    )

    with pytest.raises(ModelArtifactConflictError):
        await load_or_accept_intent(**arguments)

    assert port.calls == 1


@pytest.mark.asyncio
async def test_provider_failure_persists_the_rule_fallback(
    store: Any, model_artifact_store: Any, context: Any
) -> None:
    grant, submission = await _grant(store, context)
    port = _IntentPort(ModelErrorCode.UNAUTHORIZED)

    accepted = await load_or_accept_intent(
        grant=grant,
        envelope=submission.envelope,
        context=context,
        history=(),
        interpreter=RuleBasedIntentInterpreter(),
        model=port,
        profile=ModelInvocationProfile(),
        artifacts=model_artifact_store,
    )

    assert accepted.artifact.origin == "rule"
    assert accepted.artifact.draft.source is IntentSource.USER
    assert accepted.artifact.provider is None
    assert accepted.observation is not None
    assert accepted.observation.fallback_code.value == "model_unauthorized"


@pytest.mark.asyncio
async def test_durable_runtime_uses_saved_model_intent_before_resolver() -> None:
    port = _IntentPort(_result())
    harness = RuntimeHarness(GOLDEN, intent_model=port)
    submission = harness.submission("这是一句规则无法识别的自由说法")
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

    assert outcome.status is TaskStatus.SUCCEEDED
    assert port.calls == 1
    stored = await harness.model_artifacts.load_intent(task_id=view.task_id)
    assert stored is not None and stored.draft.source is IntentSource.MODEL
    stages = [(event.stage, event.outcome) for event in harness.sink.events]
    assert (PipelineStage.MODEL, StageOutcome.OK) in stages
    assert stages.index((PipelineStage.MODEL, StageOutcome.OK)) < stages.index(
        (PipelineStage.INTENT, StageOutcome.OK)
    )


@pytest.mark.asyncio
async def test_provider_failure_is_one_model_root_cause_then_rule_success() -> None:
    port = _IntentPort(ModelErrorCode.UNAUTHORIZED)
    harness = RuntimeHarness(GOLDEN, intent_model=port)
    submission = harness.submission("最近30分钟有哪些慢查询")
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

    assert outcome.status is TaskStatus.SUCCEEDED
    failed = [event.stage for event in harness.sink.events if event.outcome is StageOutcome.FAILED]
    assert failed == [PipelineStage.MODEL]
    intent = next(event for event in harness.sink.events if event.stage is PipelineStage.INTENT)
    assert intent.outcome is StageOutcome.OK


@pytest.mark.asyncio
async def test_outer_cancel_during_intent_writes_no_artifact_plan_or_tool_call() -> None:
    port = _BlockingIntentPort()
    harness = RuntimeHarness(GOLDEN, intent_model=port)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempted = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempted.grant is not None and attempted.submission is not None
    running = asyncio.create_task(
        harness.runtime.execute_task(
            grant=attempted.grant,
            submission=attempted.submission,
        )
    )
    await port.entered.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    assert port.cancelled.is_set()
    assert await harness.model_artifacts.load_intent(task_id=view.task_id) is None
    with pytest.raises(PlanNotFoundError):
        await harness.plan_store.load(task_id=view.task_id)
    assert harness.calls == []
    assert (await harness.store.get(lookup=harness.lookup)).status is TaskStatus.CREATED


@pytest.mark.asyncio
async def test_compatibility_handle_never_calls_or_persists_a_model() -> None:
    port = _IntentPort(_result())
    harness = RuntimeHarness(GOLDEN, intent_model=port)

    payload = await harness.handle("最近30分钟有哪些慢查询")

    assert payload.status is TaskStatus.SUCCEEDED
    assert port.calls == 0
    assert await harness.model_artifacts.load_intent(task_id=harness.task_id) is None


@pytest.mark.asyncio
async def test_intent_drift_after_committed_evidence_closes_as_recovery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _IntentPort(_result())
    harness = RuntimeHarness(GOLDEN, intent_model=port)
    submission = harness.submission("这是一句规则无法识别的自由说法")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempted = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempted.grant is not None and attempted.submission is not None
    original_transition = harness.store.transition
    failed = False

    async def fail_first_terminal_transition(*, command: Any) -> Any:
        nonlocal failed
        if command.to_status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
            TaskStatus.INDETERMINATE,
        } and not failed:
            failed = True
            raise PersistenceUnavailableError(
                category=PersistenceUnavailableCategory.TRANSIENT,
                write_outcome=PersistenceWriteOutcome.ROLLED_BACK,
            )
        return await original_transition(command=command)

    monkeypatch.setattr(harness.store, "transition", fail_first_terminal_transition)
    with pytest.raises(PersistenceUnavailableError):
        await harness.runtime.execute_task(
            grant=attempted.grant,
            submission=attempted.submission,
        )
    assert await harness.ledger.load(task_id=view.task_id)
    gateway_calls = len(harness.calls)
    artifact = await harness.model_artifacts.load_intent(task_id=view.task_id)
    assert artifact is not None
    harness.state.accepted_intents[view.task_id] = artifact.model_copy(
        update={"input_digest": "f" * 64}
    )

    outcome = await harness.runtime.execute_task(
        grant=attempted.grant,
        submission=attempted.submission,
    )

    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == "recovery_drift"
    assert port.calls == 1
    assert len(harness.calls) == gateway_calls

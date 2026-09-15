"""慢查询 advisory 的 plan budget、持久化与终态展示契约。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from enum import StrEnum
from typing import Any

import pytest
from tests.fakes.admission import slow_query_plan
from tests.fakes.recordings import EMPTY_WITHOUT_TRAFFIC, GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application import model_advisory as model_advisory_module
from xiaowei_agent.application import runtime as runtime_module
from xiaowei_agent.application.model_advisory import request_model_advisory
from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.application.task_view_runtime import assess_evidence
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    AdvisoryModelResult,
    AttemptIntent,
    ModelAdvisory,
    ModelCallKind,
    ModelErrorCode,
    ModelInvocationProfile,
    ModelUsage,
    PipelineStage,
    PlanBudget,
    SlowQueryAdvisoryRequest,
    StageOutcome,
    TaskOutcome,
    TaskStatus,
)
from xiaowei_agent.persistence import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.model_artifacts import ModelArtifactConflictError
from xiaowei_agent.persistence.store import TaskAttemptCommand


class _AdvisoryPort:
    def __init__(self, result: AdvisoryModelResult | ModelErrorCode) -> None:
        self.result = result
        self.calls: list[tuple[SlowQueryAdvisoryRequest, int]] = []

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: int,
    ) -> AdvisoryModelResult:
        self.calls.append((request, max_output_tokens))
        if isinstance(self.result, ModelErrorCode):
            raise ModelPortError(self.result)
        return self.result


class _BlockingAdvisoryPort:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: int,
    ) -> AdvisoryModelResult:
        del request, max_output_tokens
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")


class _FutureModelErrorCode(StrEnum):
    FUTURE_PROVIDER_ERROR = "model_future_provider_error"


class _UnknownErrorAdvisoryPort:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: int,
    ) -> AdvisoryModelResult:
        del request, max_output_tokens
        self.calls += 1
        raise ModelPortError(_FutureModelErrorCode.FUTURE_PROVIDER_ERROR)  # type: ignore[arg-type]


def _result() -> AdvisoryModelResult:
    return AdvisoryModelResult(
        advisory=ModelAdvisory(
            analysis="扫描行数偏高",
            suggestions=("检查分区裁剪",),
            uncertainties=(),
        ),
        usage=ModelUsage(input_tokens=20, output_tokens=8),
    )


def _request() -> SlowQueryAdvisoryRequest:
    return SlowQueryAdvisoryRequest(rows=({"queryId": "q-1"},), sampled=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_limit", "expected"),
    [(1_234, 1_234), (9_000, 4_000)],
)
async def test_advisory_output_is_bounded_by_current_plan_and_fixed_ceiling(
    plan_limit: int, expected: int
) -> None:
    port = _AdvisoryPort(_result())
    plan = slow_query_plan().model_copy(
        update={
            "budget": PlanBudget(
                max_steps=2,
                max_tool_calls=2,
                max_model_tokens=plan_limit,
            )
        }
    )

    stage = await request_model_advisory(
        request=_request(),
        model=port,
        profile=ModelInvocationProfile(),
        plan=plan,
    )

    assert stage.result == _result()
    assert port.calls == [(_request(), expected)]
    assert expected <= plan.budget.max_model_tokens


@pytest.mark.asyncio
async def test_advisory_provider_failure_has_no_result_and_no_retry() -> None:
    port = _AdvisoryPort(ModelErrorCode.SERVER_ERROR)

    stage = await request_model_advisory(
        request=_request(),
        model=port,
        profile=ModelInvocationProfile(),
        plan=slow_query_plan(),
    )

    assert stage.result is None
    assert len(port.calls) == 1
    assert stage.observation.fallback_code.value == "model_server_error"


@pytest.mark.asyncio
async def test_unknown_advisory_error_degrades_without_escaping_the_stage() -> None:
    port = _UnknownErrorAdvisoryPort()

    stage = await request_model_advisory(
        request=_request(),
        model=port,
        profile=ModelInvocationProfile(),
        plan=slow_query_plan(),
    )

    assert stage.result is None
    assert port.calls == 1
    assert stage.observation.fallback_code is not None
    assert stage.observation.fallback_code.value == "model_unavailable"


@pytest.mark.asyncio
async def test_advisory_output_that_changes_under_redaction_is_rejected() -> None:
    unsafe = AdvisoryModelResult(
        advisory=ModelAdvisory(
            analysis="token=" + "synthetic-value",
            suggestions=(),
            uncertainties=(),
        ),
        usage=ModelUsage(input_tokens=10, output_tokens=3),
    )
    port = _AdvisoryPort(unsafe)

    stage = await request_model_advisory(
        request=_request(),
        model=port,
        profile=ModelInvocationProfile(),
        plan=slow_query_plan(),
    )

    assert stage.result is None
    assert stage.observation.fallback_code.value == "model_invalid_response"


async def _execute(harness: RuntimeHarness) -> Any:
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
    return await harness.runtime.execute_task(
        grant=attempt.grant, submission=attempt.submission
    )


@pytest.mark.asyncio
async def test_runtime_saves_advisory_before_terminal_and_task_view_reuses_it() -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)

    outcome = await _execute(harness)
    first = await harness.runtime.query_task(lookup=harness.lookup)
    second = await harness.runtime.query_task(lookup=harness.lookup)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert len(port.calls) == 1
    assert port.calls[0][1] == 4_000
    artifact = await harness.model_artifacts.load_advisory(task_id=harness.task_id)
    assert artifact is not None
    assert first == second
    assert first.render is not None
    assert first.render.sections[-1].title == "模型分析（仅供参考）"
    assert len(port.calls) == 1
    stages = harness.sink.events
    reflection_index = next(
        index
        for index, event in enumerate(stages)
        if event.stage is PipelineStage.REFLECTION
    )
    advisory_index = next(
        index
        for index, event in enumerate(stages)
        if event.stage is PipelineStage.MODEL
        and event.model is not None
        and event.model.call_kind is ModelCallKind.ADVISORY
    )
    lifecycle_index = max(
        index
        for index, event in enumerate(stages)
        if event.stage is PipelineStage.LIFECYCLE
    )
    assert reflection_index < advisory_index < lifecycle_index


@pytest.mark.asyncio
async def test_advisory_failure_keeps_the_deterministic_success_payload() -> None:
    port = _AdvisoryPort(ModelErrorCode.SERVER_ERROR)
    modeled = RuntimeHarness(GOLDEN, slow_query_advisory=port)
    baseline = RuntimeHarness(GOLDEN)

    modeled_outcome = await _execute(modeled)
    baseline_outcome = await _execute(baseline)
    modeled_view = await modeled.runtime.query_task(lookup=modeled.lookup)
    baseline_view = await baseline.runtime.query_task(lookup=baseline.lookup)

    assert modeled_outcome.status is baseline_outcome.status is TaskStatus.SUCCEEDED
    assert modeled_view.render is not None and baseline_view.render is not None
    assert modeled_view.render.answer == baseline_view.render.answer
    assert modeled_view.render.status is baseline_view.render.status
    assert modeled_view.render.next_steps == baseline_view.render.next_steps
    assert tuple(
        (section.title, section.body) for section in modeled_view.render.sections
    ) == tuple(
        (section.title, section.body) for section in baseline_view.render.sections
    )
    assert await modeled.model_artifacts.load_advisory(task_id=modeled.task_id) is None
    assert len(port.calls) == 1
    assert [
        event.stage
        for event in modeled.sink.events
        if event.outcome is StageOutcome.FAILED
    ] == [PipelineStage.MODEL]


@pytest.mark.asyncio
async def test_insufficient_evidence_never_calls_advisory() -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(EMPTY_WITHOUT_TRAFFIC, slow_query_advisory=port)

    outcome = await _execute(harness)

    assert outcome.status is TaskStatus.INDETERMINATE
    assert port.calls == []
    assert await harness.model_artifacts.load_advisory(task_id=harness.task_id) is None


@pytest.mark.asyncio
async def test_compatibility_handle_uses_the_durable_advisory_path() -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)

    payload = await harness.handle("最近30分钟有哪些慢查询")

    assert payload.status is TaskStatus.SUCCEEDED
    assert len(port.calls) == 1
    stored = await harness.model_artifacts.load_advisory(task_id=harness.task_id)
    assert stored is not None


@pytest.mark.asyncio
async def test_advisory_timeout_keeps_the_deterministic_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingPort:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_advisory(
            self,
            request: SlowQueryAdvisoryRequest,
            *,
            max_output_tokens: int,
        ) -> AdvisoryModelResult:
            del request, max_output_tokens
            self.calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    port = BlockingPort()
    monkeypatch.setattr(
        model_advisory_module,
        "_stage_timeout",
        lambda _: asyncio.timeout(0.01),
    )
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)

    outcome = await _execute(harness)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert port.calls == 1
    event = next(
        event
        for event in harness.sink.events
        if event.stage is PipelineStage.MODEL
        and event.model is not None
        and event.model.call_kind is ModelCallKind.ADVISORY
    )
    assert event.model.fallback_code.value == "model_application_timeout"


@pytest.mark.asyncio
async def test_outer_cancel_during_advisory_leaves_running_fact_without_artifact() -> None:
    port = _BlockingAdvisoryPort()
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)
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
    assert await harness.model_artifacts.load_advisory(task_id=view.task_id) is None
    assert (await harness.store.get(lookup=harness.lookup)).status is TaskStatus.RUNNING
    assert len(harness.calls) == 1


@pytest.mark.asyncio
async def test_recovery_after_advisory_save_reuses_artifact_and_committed_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)
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
    original_transition = harness.store.transition
    failed = False

    async def fail_first_terminal_transition(*, command: Any) -> Any:
        nonlocal failed
        if command.to_status in TERMINAL_STATUSES and not failed:
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
    gateway_calls = len(harness.calls)
    assert await harness.model_artifacts.load_advisory(task_id=view.task_id) is not None
    preterminal = await harness.runtime.query_task(lookup=harness.lookup)
    assert preterminal.status is TaskStatus.RUNNING
    assert preterminal.render is None
    assert len(port.calls) == 1

    outcome = await harness.runtime.execute_task(
        grant=attempted.grant,
        submission=attempted.submission,
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert len(port.calls) == 1
    assert len(harness.calls) == gateway_calls


@pytest.mark.asyncio
async def test_failure_before_advisory_save_permits_only_advisory_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)
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
    original_save = harness.model_artifacts.save_advisory
    failed = False

    async def fail_before_save(*, grant: Any, candidate: Any) -> Any:
        nonlocal failed
        if not failed:
            failed = True
            raise PersistenceUnavailableError(
                category=PersistenceUnavailableCategory.TRANSIENT,
                write_outcome=PersistenceWriteOutcome.ROLLED_BACK,
            )
        return await original_save(grant=grant, candidate=candidate)

    monkeypatch.setattr(harness.model_artifacts, "save_advisory", fail_before_save)

    with pytest.raises(PersistenceUnavailableError):
        await harness.runtime.execute_task(
            grant=attempted.grant,
            submission=attempted.submission,
        )
    gateway_calls = len(harness.calls)
    assert await harness.model_artifacts.load_advisory(task_id=view.task_id) is None

    outcome = await harness.runtime.execute_task(
        grant=attempted.grant,
        submission=attempted.submission,
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert len(port.calls) == 2
    assert len(harness.calls) == gateway_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        {"input_digest": "e" * 64},
        {"result_digest": "f" * 64},
        {"provider": "drifted-provider"},
        {"model": "drifted-model"},
        {"provider_origin": "https://drifted.example.invalid"},
        {"prompt_revision": "drifted-prompt"},
        {"schema_revision": "drifted-schema"},
    ],
)
async def test_task_view_hides_advisory_after_digest_or_identity_drift(
    update: dict[str, str],
) -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)
    await _execute(harness)
    artifact = await harness.model_artifacts.load_advisory(task_id=harness.task_id)
    assert artifact is not None
    harness.state.model_advisories[harness.task_id] = artifact.model_copy(
        update=update
    )

    view = await harness.runtime.query_task(lookup=harness.lookup)

    assert view.render is not None
    assert all(
        section.title != "模型分析（仅供参考）"
        for section in view.render.sections
    )
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_task_view_hides_saved_advisory_for_a_non_success_terminal() -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)
    await _execute(harness)
    record = harness.state.tasks[harness.task_id]
    harness.state.tasks[harness.task_id] = record.model_copy(
        update={"status": TaskStatus.FAILED, "terminal_reason": "test_failure"}
    )

    view = await harness.runtime.query_task(lookup=harness.lookup)

    assert view.status is TaskStatus.FAILED
    assert view.render is not None
    assert all(
        section.title != "模型分析（仅供参考）"
        for section in view.render.sections
    )
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_task_view_status_gate_does_not_trust_a_capability_projector() -> None:
    port = _AdvisoryPort(_result())
    harness = RuntimeHarness(GOLDEN, slow_query_advisory=port)
    await _execute(harness)
    stored_plan = await harness.plan_store.load(task_id=harness.task_id)
    evidences = await harness.ledger.load(task_id=harness.task_id)
    binding = harness.runtime._bindings.runtime_for_plan(plan=stored_plan.plan)
    verdict = assess_evidence(binding=binding, evidences=evidences)
    assert binding.advisory_projector is not None
    request = binding.advisory_projector(
        task_id=harness.task_id,
        plan=stored_plan.plan,
        outcome=TaskOutcome(
            task_id=harness.task_id,
            status=TaskStatus.SUCCEEDED,
            terminal_reason=None,
            evidence_refs=tuple(item.evidence_id for item in evidences),
            render_ref=None,
        ),
        evidences=evidences,
        verdict=verdict,
    )
    assert request is not None

    permissive = replace(binding, advisory_projector=lambda **_: request)

    class _PermissiveBindings:
        def runtime_for_plan(self, *, plan: Any) -> Any:
            del plan
            return permissive

    harness.runtime._task_views._bindings = _PermissiveBindings()  # type: ignore[assignment]
    record = harness.state.tasks[harness.task_id]
    harness.state.tasks[harness.task_id] = record.model_copy(
        update={"status": TaskStatus.FAILED, "terminal_reason": "test_failure"}
    )

    view = await harness.runtime.query_task(lookup=harness.lookup)

    assert view.status is TaskStatus.FAILED
    assert view.render is not None
    assert all(
        section.title != "模型分析（仅供参考）"
        for section in view.render.sections
    )


@pytest.mark.asyncio
async def test_advisory_conflict_is_reflected_from_the_final_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def conflict(**kwargs: Any) -> Any:
        raise ModelArtifactConflictError(
            "stored model advisory conflicts with rebuilt input",
            task_id=kwargs["task_id"],
        )

    monkeypatch.setattr(runtime_module, "load_or_accept_advisory", conflict)
    harness = RuntimeHarness(
        EMPTY_WITHOUT_TRAFFIC,
        slow_query_advisory=_AdvisoryPort(_result()),
    )

    outcome = await _execute(harness)

    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == "recovery_drift"
    reflection = next(
        event
        for event in reversed(harness.sink.events)
        if event.stage is PipelineStage.REFLECTION
    )
    assert reflection.outcome is StageOutcome.OK

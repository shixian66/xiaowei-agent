"""无执行权任务投影 Runtime 的共享行为契约。"""

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application import task_view_runtime as task_view_module
from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingError,
    CapabilityRuntimeBinding,
)
from xiaowei_agent.application.task_view_runtime import (
    TaskViewRuntime,
    assess_evidence,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    ClarificationField,
    ClarificationPayload,
    ClarificationReasonCode,
    EffectClass,
    EvidenceEnvelope,
    ExecutionDisclosure,
    ExecutionDisclosureDisposition,
    ExecutionDisclosureStep,
    ModelAdvisory,
    ModelInvocationProfile,
    ReadClass,
    RenderPayload,
    TaskRecord,
    TaskStatus,
)


def _task_views(harness: RuntimeHarness) -> TaskViewRuntime:
    return TaskViewRuntime(
        task_store=harness.store,
        plan_store=harness.plan_store,
        ledger=harness.ledger,
        bindings=harness.runtime._bindings,
        snapshot=StaticCapabilityRegistry().snapshot(),
        model_artifacts=harness.model_artifacts,
        model_profile=ModelInvocationProfile(),
    )


def test_public_evidence_assessor_preserves_preplan_rejection_semantics() -> None:
    verdict = assess_evidence(binding=None, evidences=())

    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True
    assert verdict.needs_user_input is False
    assert tuple(item.key for item in verdict.missing) == ("execution_plan",)


async def test_public_evidence_assessor_rejects_evidence_without_a_plan() -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    evidences = await harness.ledger.load(task_id=harness.task_id)

    with pytest.raises(
        CapabilityBindingError,
        match="evidence exists without a capability plan",
    ):
        assess_evidence(binding=None, evidences=evidences)


async def test_full_and_narrow_runtimes_share_the_terminal_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    narrow = _task_views(harness)
    original = task_view_module.project_terminal
    calls = 0

    def counting_projection(
        *,
        record: TaskRecord,
        evidences: tuple[EvidenceEnvelope, ...],
        verdict: AnswerabilityVerdict,
        binding: CapabilityRuntimeBinding,
        advisory: ModelAdvisory | None = None,
    ) -> RenderPayload:
        nonlocal calls
        calls += 1
        return original(
            record=record,
            evidences=evidences,
            verdict=verdict,
            binding=binding,
            advisory=advisory,
        )

    monkeypatch.setattr(task_view_module, "project_terminal", counting_projection)

    full_view = await harness.runtime.query_task(lookup=harness.lookup)
    narrow_view = await narrow.query_task(lookup=harness.lookup)

    assert full_view == narrow_view
    assert calls == 2


async def test_narrow_runtime_submit_only_persists_a_task() -> None:
    harness = RuntimeHarness(GOLDEN)
    narrow = _task_views(harness)

    view = await narrow.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )

    assert view.status is TaskStatus.CREATED
    assert view.render is None
    assert harness.gateway.invocations == 0
    assert await harness.store.load_step_executions(task_id=view.task_id) == ()


async def test_narrow_runtime_replays_an_existing_terminal_submission_without_execution(
) -> None:
    harness = RuntimeHarness(GOLDEN)
    rendered = await harness.handle("最近30分钟有哪些慢查询")
    invocations = harness.gateway.invocations
    narrow = _task_views(harness)

    replayed = await narrow.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )

    assert replayed.status is TaskStatus.SUCCEEDED
    assert replayed.render == rendered
    assert harness.gateway.invocations == invocations


def test_task_view_uses_clarification_payload_for_clarification_terminal() -> None:
    payload = ClarificationPayload(
        reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
        missing_fields=(ClarificationField.TIME_RANGE,),
        prompt="请补充时间范围。",
    )

    view = task_view_module.TaskView(
        task_id="task-clarify",
        status=TaskStatus.CLARIFICATION_REQUIRED,
        clarification=payload,
        query_path="/v1/tasks/task-clarify",
    )

    assert view.render is None
    assert view.clarification == payload


def test_task_view_allows_disclosure_independent_of_terminal_projection() -> None:
    disclosure = ExecutionDisclosure(
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        environment_id="dev",
        provider="starrocks",
        resource_kind="cluster",
        resource_ids=("starrocks-dev-1",),
        pure_read_only=True,
        plan_disposition=ExecutionDisclosureDisposition.BOUNDED_READ,
        read_classes=(ReadClass.BOUNDED,),
        has_side_effect=False,
        steps=(
            ExecutionDisclosureStep(
                step_id="s1",
                operation="list_slow_queries",
                effect_class=EffectClass.READ,
                read_class=ReadClass.BOUNDED,
                side_effect=False,
            ),
        ),
        external_target_access=True,
    )

    view = task_view_module.TaskView(
        task_id="task-running",
        status=TaskStatus.RUNNING,
        disclosure=disclosure,
        query_path="/v1/tasks/task-running",
    )

    assert view.render is None
    assert view.clarification is None
    assert view.disclosure == disclosure


async def test_terminal_task_view_includes_disclosure_from_stored_plan() -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")

    view = await harness.runtime.query_task(lookup=harness.lookup)

    assert view.disclosure is not None
    assert view.disclosure.capability_id == "starrocks.slow_query.diagnose"
    assert view.disclosure.environment_id == harness.context.environment_id
    assert view.disclosure.provider == "starrocks"
    assert view.disclosure.plan_disposition is ExecutionDisclosureDisposition.BOUNDED_READ


def test_task_view_rejects_mismatched_render_and_clarification_shapes() -> None:
    clarification = ClarificationPayload(
        reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
        missing_fields=(ClarificationField.TIME_RANGE,),
        prompt="请补充时间范围。",
    )
    render = RenderPayload(
        answer="已完成",
        sections=(),
        next_steps=("继续观察。",),
        status=TaskStatus.SUCCEEDED,
        refs=(),
    )

    with pytest.raises(ValueError, match="clarification presence"):
        task_view_module.TaskView(
            task_id="task-1",
            status=TaskStatus.CLARIFICATION_REQUIRED,
            render=render,
            clarification=clarification,
            query_path="/v1/tasks/task-1",
        )
    with pytest.raises(ValueError, match="clarification presence"):
        task_view_module.TaskView(
            task_id="task-2",
            status=TaskStatus.SUCCEEDED,
            render=render,
            clarification=clarification,
            query_path="/v1/tasks/task-2",
        )
    with pytest.raises(ValueError, match="render presence"):
        task_view_module.TaskView(
            task_id="task-3",
            status=TaskStatus.SUCCEEDED,
            query_path="/v1/tasks/task-3",
        )

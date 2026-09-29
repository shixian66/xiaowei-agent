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
from xiaowei_agent.governance.sql_message import recognize_sql_message
from xiaowei_agent.persistence.plans import PlanNotFoundError
from xiaowei_agent.persistence.store import TransitionCommand


def _task_views(harness: RuntimeHarness) -> TaskViewRuntime:
    return TaskViewRuntime(
        task_store=harness.store,
        plan_store=harness.plan_store,
        ledger=harness.ledger,
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=harness.runtime._rendering_bindings,
        recognize_sql=recognize_sql_message,
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


async def test_conversation_projection_requires_the_conversation_terminal_reason() -> (
    None
):
    """只有 `interaction.conversation_responded` 才配拿到能力目录。

    这条守卫此前**不承重**：去掉 `terminal_reason` 判断后全量仍然全绿，因为当前
    没有别的路径会产出"SUCCEEDED 且无 plan 无 evidence"的任务。但那只是今天的
    巧合——真正的风险是将来某条新路径这样收口，然后毫无征兆地拿到一份"我能做的
    事就是下面这份能力清单"，而它其实根本没做能力目录这件事。
    """
    harness = RuntimeHarness(GOLDEN)
    views = _task_views(harness)
    record = await harness.store.create_task(
        submission=harness.submission("你能做什么？")
    )
    grant = await harness.store.acquire_lease(
        task_id=record.task_id, owner="probe", ttl_seconds=60
    )
    assert grant is not None
    current = record
    for status in (
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
    ):
        result = await harness.store.transition(
            command=TransitionCommand(
                task_id=record.task_id,
                expected_version=current.version,
                to_status=status,
                fencing_token=grant.fencing_token,
                # 终态原因是别的东西，不是对话。
                terminal_reason=(
                    "some.other.terminal.reason"
                    if status is TaskStatus.SUCCEEDED
                    else None
                ),
            )
        )
        assert result.applied, result.rejection
        current = result.winner

    with pytest.raises(PlanNotFoundError):
        await views.project_recorded(record=current)


def test_sql_artifact_failures_map_to_two_closed_codes() -> None:
    from xiaowei_agent.application.task_view_runtime import (
        ApplicationFailure,
        classify_application_exception,
    )
    from xiaowei_agent.persistence.store import (
        SqlArtifactExpiredError,
        SqlArtifactUnavailableError,
        SqlArtifactUnavailableReason,
    )

    assert classify_application_exception(SqlArtifactExpiredError()) is (
        ApplicationFailure.SQL_ARTIFACT_EXPIRED
    )
    for reason in SqlArtifactUnavailableReason:
        # 三种内部原因对入口不可区分，不泄漏对象是否存在或属于谁。
        assert classify_application_exception(
            SqlArtifactUnavailableError(reason=reason)
        ) is ApplicationFailure.SQL_ARTIFACT_UNAVAILABLE
    assert ApplicationFailure.SQL_ARTIFACT_EXPIRED.value == "sql_artifact.expired"
    assert ApplicationFailure.SQL_ARTIFACT_UNAVAILABLE.value == "sql_artifact.unavailable"


@pytest.mark.parametrize(
    ("terminal_reason", "embedded"),
    [
        ("interaction.embedded_sql_not_executed", True),
        ("interaction.route_not_available", False),
        ("capability.fields_invalid", False),
        (None, False),
    ],
)
async def test_preplan_rejection_projection_uses_the_recorded_reason(
    store, memory_state, context, terminal_reason: str | None, embedded: bool
) -> None:
    from tests.conftest import drive_to_terminal, lookup_for, make_submission

    from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
    from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
    from xiaowei_agent.persistence.plans import InMemoryPlanStore
    from xiaowei_agent.rendering.generic import EMBEDDED_SQL_REJECTED

    record = await store.create_task(submission=make_submission(context))
    await drive_to_terminal(store, lookup_for(record), TaskStatus.REJECTED)
    current = await store.get(lookup=lookup_for(record))
    rejected = current.model_copy(update={"terminal_reason": terminal_reason})
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),  # type: ignore[arg-type]
        recognize_sql=recognize_sql_message,
    )

    payload = await runtime.project_recorded(record=rejected)

    assert payload.status is TaskStatus.REJECTED
    assert (payload.answer == EMBEDDED_SQL_REJECTED) is embedded
    if not embedded:
        assert payload.answer == "请求在执行前被拒绝，未调用任何工具。"

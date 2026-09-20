"""I2 普通对话在真实 PostgreSQL 上的生命周期。

对话任务没有 plan、没有 evidence，它的全部事实就是 tasks 行上的状态/终态原因和
审计事件。这条链路此前只在内存 fake 上验证过——而 I1-C 的 V1 plan 假绿正是只在
真实库上才暴露出来的，所以这条必须落到真库。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import make_envelope, make_lookup, make_submission
from tests.fakes.admission import CONTEXT, POLICY_SNAPSHOT
from tests.fakes.capability_bindings import build_test_capability_bindings
from tests.fakes.clock import ManualClock
from tests.fakes.recordings import GOLDEN
from tests.fakes.runner import CountingApprovalGate, CountingGateway
from tests.fakes.sinks import RecordingTraceSink

from xiaowei_agent.application.default_capabilities import (
    build_default_capability_bindings,
)
from xiaowei_agent.application.runtime import XiaoweiRuntime
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    AttemptIntent,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelInvocationProfile,
    ModelUsage,
    PipelineStage,
    StageOutcome,
    TaskStatus,
)
from xiaowei_agent.persistence.plans import PlanNotFoundError
from xiaowei_agent.persistence.postgres import (
    PostgresClarificationRecordStore,
    PostgresEvidenceLedger,
    PostgresModelArtifactStore,
    PostgresPlanStore,
    PostgresTaskStore,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.rendering.generic import CONVERSATION_TERMINAL_REASON
from xiaowei_agent.runners.deterministic import DeterministicStepRunner
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter


class _ConversationPort:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, request: Any) -> InteractionModelResult:
        self.calls += 1
        return InteractionModelResult(
            draft=InteractionDraft(
                proposed_kind=InteractionKind.CONVERSATION,
                capability_draft=None,
                confidence=0.8,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(),
        )


class _Stack:
    def __init__(self, engine: AsyncEngine, clock: ManualClock) -> None:
        self.task_store = PostgresTaskStore(engine=engine, clock=clock)
        self.plan_store = PostgresPlanStore(engine=engine)
        self.ledger = PostgresEvidenceLedger(engine=engine)
        snapshot = StaticCapabilityRegistry().snapshot()
        self.snapshot = snapshot
        self.gateway = CountingGateway(
            DeterministicToolGateway(
                adapters={"starrocks": StarRocksRecordingAdapter(GOLDEN)}
            )
        )
        self.approval_gate = CountingApprovalGate()
        self.sink = RecordingTraceSink()
        self.port = _ConversationPort()
        self.runtime = XiaoweiRuntime(
            interpreter=RuleBasedIntentInterpreter(),
            resolver=DeterministicCapabilityResolver(),
            snapshot=snapshot,
            bindings=build_default_capability_bindings(
                snapshot=snapshot, policy_snapshot=POLICY_SNAPSHOT
            ),
            task_store=self.task_store,
            plan_store=self.plan_store,
            ledger=self.ledger,
            runner=DeterministicStepRunner(
                task_store=self.task_store,
                plan_store=self.plan_store,
                ledger=self.ledger,
                gateway=self.gateway,
                approval_gate=self.approval_gate,
                snapshot=snapshot,
                policy_snapshot=POLICY_SNAPSHOT,
                bindings=build_test_capability_bindings(
                    snapshot=snapshot, policy_snapshot=POLICY_SNAPSHOT
                ),
                clock=clock,
                sink=self.sink,
            ),
            sink=self.sink,
            clock=clock,
            model_artifacts=PostgresModelArtifactStore(engine=engine, clock=clock),
            model_profile=ModelInvocationProfile(),
            clarification_records=PostgresClarificationRecordStore(
                engine=engine, clock=clock
            ),
            interaction_classifier=self.port,
        )


async def test_conversation_persists_its_whole_truth_in_postgres(
    clean_database: AsyncEngine, clock: ManualClock
) -> None:
    stack = _Stack(clean_database, clock)
    submission = make_submission(
        CONTEXT, envelope=make_envelope(text="你能做什么？"), as_of=clock()
    )
    view = await stack.runtime.submit_task(submission=submission)
    attempt = await stack.task_store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=CONTEXT.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None

    outcome = await stack.runtime.execute_task(
        grant=attempt.grant, submission=attempt.submission
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert outcome.terminal_reason == CONVERSATION_TERMINAL_REASON

    record = await stack.task_store.get(lookup=make_lookup(view.task_id, CONTEXT))
    assert record.status is TaskStatus.SUCCEEDED
    assert record.terminal_reason == CONVERSATION_TERMINAL_REASON

    # 无计划、无证据、未触工具、未触审批——这是这条通道的全部承诺。
    assert await stack.ledger.load(task_id=view.task_id) == ()
    assert stack.gateway.invocations == 0
    assert stack.approval_gate.calls == 0
    try:
        await stack.plan_store.load(task_id=view.task_id)
    except PlanNotFoundError:
        pass
    else:  # pragma: no cover - 只在回归时触发
        raise AssertionError("conversation must not persist a plan")

    # 回答从库里的任务事实重新投影，且列出当前快照的每一条能力。
    projected = await stack.runtime.query_task(
        lookup=make_lookup(view.task_id, CONTEXT)
    )
    assert projected.disclosure is None
    assert projected.render is not None
    assert projected.render.refs == (
        f"capability-snapshot:{stack.snapshot.snapshot_id}",
    )
    assert [section.title for section in projected.render.sections] == [
        spec.capability_id for spec in stack.snapshot.specs
    ]

    reflections = [
        event
        for event in stack.sink.events
        if event.stage is PipelineStage.REFLECTION
    ]
    assert [event.outcome for event in reflections] == [StageOutcome.OK]


async def test_conversation_resumes_after_a_mid_flight_crash(
    clean_database: AsyncEngine, clock: ManualClock
) -> None:
    """崩在 RUNNING 之后：新 attempt 必须把它收干净，而不是重跑或卡死。

    对话的迁移路径由**当前状态**推导，不是固定三跳；写死三跳时这条会在
    CREATED→PLANNING 上被状态机拒绝，任务永远留在 RUNNING。
    """
    stack = _Stack(clean_database, clock)
    submission = make_submission(
        CONTEXT, envelope=make_envelope(text="你能做什么？"), as_of=clock()
    )
    view = await stack.runtime.submit_task(submission=submission)
    first = await stack.task_store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-crashed",
            ttl_seconds=60,
            trace_id=CONTEXT.trace_id,
        )
    )
    assert first.grant is not None
    record = await stack.task_store.get(lookup=make_lookup(view.task_id, CONTEXT))
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING):
        result = await stack.task_store.transition(
            command=TransitionCommand(
                task_id=view.task_id,
                expected_version=record.version,
                to_status=status,
                fencing_token=first.grant.fencing_token,
            )
        )
        assert result.applied, result.rejection
        record = result.winner
    assert record.status is TaskStatus.RUNNING

    clock.advance(seconds=300)
    second = await stack.task_store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-recovering",
            ttl_seconds=60,
            trace_id=CONTEXT.trace_id,
        )
    )
    assert second.grant is not None

    outcome = await stack.runtime.execute_task(
        grant=second.grant, submission=second.submission or submission
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert outcome.terminal_reason == CONVERSATION_TERMINAL_REASON
    # insert-once interaction artifact 被复用：恢复不得再问一次模型。
    assert stack.port.calls == 1
    assert stack.gateway.invocations == 0

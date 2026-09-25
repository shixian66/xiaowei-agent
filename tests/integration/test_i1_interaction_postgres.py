"""I1 interaction lifecycle over real PostgreSQL persistence."""

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
    ModelInvocationProfile,
    PipelineStage,
    TaskStatus,
)
from xiaowei_agent.persistence.postgres import (
    PostgresClarificationRecordStore,
    PostgresEvidenceLedger,
    PostgresModelArtifactStore,
    PostgresPlanStore,
    PostgresTaskStore,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand
from xiaowei_agent.runners.deterministic import DeterministicStepRunner
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter


class _PostgresI1Stack:
    def __init__(
        self,
        *,
        runtime: XiaoweiRuntime,
        task_store: PostgresTaskStore,
        plan_store: PostgresPlanStore,
        artifacts: PostgresModelArtifactStore,
        gateway: CountingGateway,
        sink: RecordingTraceSink,
    ) -> None:
        self.runtime = runtime
        self.task_store = task_store
        self.plan_store = plan_store
        self.artifacts = artifacts
        self.gateway = gateway
        self.sink = sink


def _runtime(clean_database: AsyncEngine, clock: ManualClock) -> _PostgresI1Stack:
    task_store = PostgresTaskStore(engine=clean_database, clock=clock)
    plan_store = PostgresPlanStore(engine=clean_database)
    ledger = PostgresEvidenceLedger(engine=clean_database)
    artifacts = PostgresModelArtifactStore(engine=clean_database, clock=clock)
    clarifications = PostgresClarificationRecordStore(
        engine=clean_database,
        clock=clock,
    )
    snapshot = StaticCapabilityRegistry().snapshot()
    gateway = CountingGateway(
        DeterministicToolGateway(
            adapters={"starrocks": StarRocksRecordingAdapter(GOLDEN)}
        )
    )
    sink = RecordingTraceSink()
    runner = DeterministicStepRunner(
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        gateway=gateway,
        approval_gate=CountingApprovalGate(),
        snapshot=snapshot,
        policy_snapshot=POLICY_SNAPSHOT,
        bindings=build_test_capability_bindings(
            snapshot=snapshot,
            policy_snapshot=POLICY_SNAPSHOT,
        ),
        clock=clock,
        sink=sink,
    )
    runtime_bindings = build_default_capability_bindings(
        snapshot=snapshot,
        policy_snapshot=POLICY_SNAPSHOT,
    )
    runtime = XiaoweiRuntime(
        interpreter=RuleBasedIntentInterpreter(),
        resolver=DeterministicCapabilityResolver(),
        snapshot=snapshot,
        bindings=runtime_bindings,
        rendering_bindings=runtime_bindings,
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        runner=runner,
        sink=sink,
        clock=clock,
        model_artifacts=artifacts,
        model_profile=ModelInvocationProfile(),
        clarification_records=clarifications,
    )
    return _PostgresI1Stack(
        runtime=runtime,
        task_store=task_store,
        plan_store=plan_store,
        artifacts=artifacts,
        gateway=gateway,
        sink=sink,
    )


async def test_root_to_gateway_lifecycle_persists_i1_facts_in_postgres(
    clean_database: AsyncEngine,
    clock: ManualClock,
) -> None:
    stack = _runtime(clean_database, clock)
    submission = make_submission(
        CONTEXT,
        envelope=make_envelope(text="最近30分钟有哪些慢查询"),
        as_of=clock(),
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
        grant=attempt.grant,
        submission=attempt.submission,
    )

    assert outcome.status is TaskStatus.SUCCEEDED
    assert await stack.artifacts.load_interaction(task_id=view.task_id)
    assert await stack.plan_store.load(task_id=view.task_id)
    projected = await stack.runtime.query_task(
        lookup=make_lookup(view.task_id, CONTEXT)
    )
    assert projected.status is TaskStatus.SUCCEEDED
    assert projected.disclosure is not None
    stages = [event.stage for event in stack.sink.events]
    assert stages.index(PipelineStage.DISCLOSURE) < stages.index(
        PipelineStage.ADMISSION
    )
    assert stages.index(PipelineStage.DISCLOSURE) < stages.index(
        PipelineStage.GATEWAY
    )
    assert stack.gateway.invocations == 1

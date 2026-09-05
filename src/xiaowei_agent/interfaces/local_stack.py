"""M5 本地装配根；fake adapter 的唯一生产代码导入点。"""

import datetime as dt
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from xiaowei_agent.application.runtime import XiaoweiRuntime
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import GATEWAY_NAME, OP_COUNT, OP_LIST, SLOW_QUERY_SURFACE
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ReadinessProbe, ReadinessReport
from xiaowei_agent.governance.approval import NeverGrantingApprovalGate
from xiaowei_agent.governance.profiles import (
    ACTIVE_POLICY_SNAPSHOT,
    SLOW_QUERY_READONLY_PROFILE,
)
from xiaowei_agent.observability.durable_sink import DurableTraceSink
from xiaowei_agent.persistence.evidence import EvidenceLedger, InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.plans import InMemoryPlanStore, PlanStore
from xiaowei_agent.persistence.store import Clock, TaskStore
from xiaowei_agent.runners.deterministic import DeterministicStepRunner
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter
from xiaowei_agent.tools.starrocks_recording import default_recording

MonotonicClock = Callable[[], float]
AsyncClose = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class LocalStack:
    runtime: XiaoweiRuntime
    task_store: TaskStore
    plan_store: PlanStore
    evidence_ledger: EvidenceLedger
    clock: Clock
    monotonic: MonotonicClock
    settings: Settings
    readiness: ReadinessProbe
    aclose: AsyncClose


class _Ready:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True,
            revision_matches_head=True,
            assembled=True,
        )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def build_in_memory_local_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
    monotonic: MonotonicClock = time.monotonic,
    on_close: Callable[[], None] | None = None,
) -> LocalStack:
    """装配不依赖 tests/ 的 fake 闭环；供打包证明与本地测试使用。"""
    state = InMemoryPersistenceState()
    task_store = InMemoryTaskStore(
        clock=clock,
        state=state,
        task_failure_limit=settings.task_failure_limit,
    )
    plan_store = InMemoryPlanStore(state=state)
    ledger = InMemoryEvidenceLedger(state=state)
    adapter = StarRocksRecordingAdapter(
        default_recording(list_operation=OP_LIST, count_operation=OP_COUNT)
    )
    gateway = DeterministicToolGateway(adapters={GATEWAY_NAME: adapter})
    sink = DurableTraceSink(writer=task_store)
    snapshot = StaticCapabilityRegistry().snapshot()
    runner = DeterministicStepRunner(
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        gateway=gateway,
        approval_gate=NeverGrantingApprovalGate(),
        snapshot=snapshot,
        policy_snapshot=ACTIVE_POLICY_SNAPSHOT,
        profile=SLOW_QUERY_READONLY_PROFILE,
        surface=SLOW_QUERY_SURFACE,
        clock=clock,
        sink=sink,
        lease_ttl_seconds=settings.lease_ttl_seconds,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
    )
    runtime = XiaoweiRuntime(
        interpreter=RuleBasedIntentInterpreter(),
        resolver=DeterministicCapabilityResolver(),
        snapshot=snapshot,
        task_store=task_store,
        ledger=ledger,
        runner=runner,
        sink=sink,
        clock=clock,
    )

    async def close() -> None:
        if on_close is not None:
            on_close()

    return LocalStack(
        runtime=runtime,
        task_store=task_store,
        plan_store=plan_store,
        evidence_ledger=ledger,
        clock=clock,
        monotonic=monotonic,
        settings=settings,
        readiness=_Ready(),
        aclose=close,
    )

"""M5 本地装配根；fake adapter 的唯一生产代码导入点。"""

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from xiaowei_agent.application.default_capabilities import (
    build_default_capability_bindings,
)
from xiaowei_agent.application.runtime import XiaoweiRuntime
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.prometheus_alert import (
    ALERTMANAGER_GATEWAY,
    OP_GET_ACTIVE_ALERTS,
    OP_QUERY_METRIC_RANGE,
    PROMETHEUS_GATEWAY,
    PROMQL_SURFACE,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import GATEWAY_NAME, OP_COUNT, OP_LIST
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    ReadinessProbe,
    ReadinessReport,
    RequestContext,
    ToolCall,
    ToolResult,
)
from xiaowei_agent.governance.approval import NeverGrantingApprovalGate
from xiaowei_agent.governance.profiles import ACTIVE_POLICY_SNAPSHOT
from xiaowei_agent.observability.durable_sink import DurableTraceSink
from xiaowei_agent.persistence.database import (
    PostgresReadinessProbe,
    create_database_engine,
)
from xiaowei_agent.persistence.evidence import EvidenceLedger, InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.plans import InMemoryPlanStore, PlanStore
from xiaowei_agent.persistence.postgres import (
    PostgresEvidenceLedger,
    PostgresPlanStore,
    PostgresTaskStore,
)
from xiaowei_agent.persistence.store import Clock, TaskStore
from xiaowei_agent.planning.prometheus.compiler import compile_promql
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.templates import (
    metric_name_for_template,
    template_for_alert,
)
from xiaowei_agent.runners.deterministic import DeterministicStepRunner
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.alertmanager_recording import default_alertmanager_recording
from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway
from xiaowei_agent.tools.prometheus_fake import (
    PrometheusRecordingAdapter,
    PrometheusRecordingKey,
)
from xiaowei_agent.tools.prometheus_recording import default_prometheus_recording
from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter
from xiaowei_agent.tools.starrocks_recording import default_recording

MonotonicClock = Callable[[], float]
AsyncClose = Callable[[], Awaitable[None]]
SMOKE_BARRIER_MARKER = "XIAOWEI_SMOKE_TOOL_RESULT_READY"


class _PostResultBarrierGateway:
    """仅由 smoke 配置装配：工具已返回后阻塞，直到进程被取消。"""

    def __init__(self, wrapped: ToolGateway) -> None:
        self._wrapped = wrapped
        self.entered = asyncio.Event()

    async def invoke(
        self,
        call: ToolCall,
        *,
        context: RequestContext,
        admission: AdmissionCertificate,
    ) -> ToolResult:
        result = await self._wrapped.invoke(
            call,
            context=context,
            admission=admission,
        )
        print(SMOKE_BARRIER_MARKER, flush=True)
        self.entered.set()
        await asyncio.Event().wait()
        return result


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
    policy_revision: str


class _Ready:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True,
            revision_matches_head=True,
            assembled=True,
        )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _assemble_local_stack(
    *,
    settings: Settings,
    task_store: TaskStore,
    plan_store: PlanStore,
    ledger: EvidenceLedger,
    readiness: ReadinessProbe,
    aclose: AsyncClose,
    clock: Clock,
    monotonic: MonotonicClock,
) -> LocalStack:
    starrocks_adapter = StarRocksRecordingAdapter(
        default_recording(list_operation=OP_LIST, count_operation=OP_COUNT)
    )
    alertmanager_adapter = AlertmanagerRecordingAdapter(
        default_alertmanager_recording(operation=OP_GET_ACTIVE_ALERTS)
    )
    recording: dict[PrometheusRecordingKey, AdapterResponse] = {}
    catalog_center = clock().astimezone(dt.UTC).replace(second=0, microsecond=0)
    for minute_offset in range(-120, 121):
        window_end = catalog_center + dt.timedelta(minutes=minute_offset)
        for alert_name, instance, values in (
            ("HostHighCpu", "node-1.example.com:9100", (82.0, 91.5)),
            ("InstanceDown", "10.0.0.8:9100", (0.0, 0.0)),
        ):
            params = PrometheusAlertParams(
                alert_name=alert_name,
                instance=instance,
                window_start=window_end - dt.timedelta(minutes=30),
                window_end=window_end,
            )
            template_id = template_for_alert(alert_name)
            recording.update(
                default_prometheus_recording(
                    operation=OP_QUERY_METRIC_RANGE,
                    promql=compile_promql(
                        template_id=template_id,
                        params=params,
                        surface=PROMQL_SURFACE,
                    ),
                    template_id=template_id,
                    alert_name=alert_name,
                    instance=instance,
                    metric_name=metric_name_for_template(template_id),
                    values=values,
                    window_start=params.window_start.isoformat(),
                    window_end=params.window_end.isoformat(),
                )
            )
    prometheus_adapter = PrometheusRecordingAdapter(recording)
    base_gateway = DeterministicToolGateway(
        adapters={
            GATEWAY_NAME: starrocks_adapter,
            ALERTMANAGER_GATEWAY: alertmanager_adapter,
            PROMETHEUS_GATEWAY: prometheus_adapter,
        }
    )
    gateway: ToolGateway = (
        _PostResultBarrierGateway(base_gateway)
        if settings.smoke_step_barrier
        else base_gateway
    )
    sink = DurableTraceSink(writer=task_store)
    snapshot = StaticCapabilityRegistry().snapshot()
    bindings = build_default_capability_bindings(
        snapshot=snapshot, policy_snapshot=ACTIVE_POLICY_SNAPSHOT
    )
    runner = DeterministicStepRunner(
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        gateway=gateway,
        approval_gate=NeverGrantingApprovalGate(),
        snapshot=snapshot,
        policy_snapshot=ACTIVE_POLICY_SNAPSHOT,
        bindings=bindings,
        clock=clock,
        sink=sink,
        lease_ttl_seconds=settings.lease_ttl_seconds,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
    )
    runtime = XiaoweiRuntime(
        interpreter=RuleBasedIntentInterpreter(),
        resolver=DeterministicCapabilityResolver(),
        snapshot=snapshot,
        bindings=bindings,
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        runner=runner,
        sink=sink,
        clock=clock,
    )

    return LocalStack(
        runtime=runtime,
        task_store=task_store,
        plan_store=plan_store,
        evidence_ledger=ledger,
        clock=clock,
        monotonic=monotonic,
        settings=settings,
        readiness=readiness,
        aclose=aclose,
        policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
    )


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

    async def close() -> None:
        if on_close is not None:
            on_close()

    return _assemble_local_stack(
        settings=settings,
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        readiness=_Ready(),
        aclose=close,
        clock=clock,
        monotonic=monotonic,
    )


async def build_postgres_local_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
    monotonic: MonotonicClock = time.monotonic,
) -> LocalStack:
    """用一个 AsyncEngine 装配 API/Worker 共用的 PostgreSQL 本地栈。"""
    engine = create_database_engine(settings)

    async def close() -> None:
        await engine.dispose()

    try:
        task_store = PostgresTaskStore(
            engine=engine,
            clock=clock,
            task_failure_limit=settings.task_failure_limit,
        )
        return _assemble_local_stack(
            settings=settings,
            task_store=task_store,
            plan_store=PostgresPlanStore(engine=engine),
            ledger=PostgresEvidenceLedger(engine=engine),
            readiness=PostgresReadinessProbe(engine=engine, assembled=True),
            aclose=close,
            clock=clock,
            monotonic=monotonic,
        )
    except Exception:
        await engine.dispose()
        raise

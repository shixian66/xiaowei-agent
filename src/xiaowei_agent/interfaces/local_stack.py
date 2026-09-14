"""本地进程装配根；按进程职责组装读取投影或完整执行依赖。"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from xiaowei_agent.application.capability_runtime import CapabilityBindingRegistry
from xiaowei_agent.application.default_capabilities import (
    build_default_capability_bindings,
)
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    CapabilitySnapshot,
    ModelInvocationProfile,
    ReadinessProbe,
    ReadinessReport,
)
from xiaowei_agent.governance.profiles import ACTIVE_POLICY_SNAPSHOT
from xiaowei_agent.persistence.database import (
    DatabaseConfigurationError,
    PostgresReadinessProbe,
    create_database_engine,
)
from xiaowei_agent.persistence.evidence import EvidenceLedger, InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryChannelStore, InMemoryTaskStore
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.model_artifacts import (
    InMemoryModelArtifactStore,
    ModelArtifactStore,
)
from xiaowei_agent.persistence.plans import InMemoryPlanStore, PlanStore
from xiaowei_agent.persistence.postgres import (
    PostgresChannelStore,
    PostgresEvidenceLedger,
    PostgresModelArtifactStore,
    PostgresPlanStore,
    PostgresTaskStore,
    PostgresWebSessionStore,
)
from xiaowei_agent.persistence.store import Clock, TaskStore

if TYPE_CHECKING:
    # 这些执行栈类型必须保持静态导入；移到顶层会让仅 import local_stack 的
    # internal-api 一并加载完整 Runtime、Runner 与工具链。代价是 LocalStack 的
    # 延迟注解不能用 get_type_hints() 无参解析；窄进程只内省 TaskViewStack。
    from xiaowei_agent.application.channel_access import (
        FeishuMembershipPort,
        TaskAccessService,
    )
    from xiaowei_agent.application.channel_projection import (
        AsyncSleep,
        ChannelMessagePort,
        ChannelProjectionService,
    )
    from xiaowei_agent.application.channel_submission import ChannelSubmissionService
    from xiaowei_agent.application.model_ports import (
        IntentModelPort,
        SlowQueryAdvisoryPort,
    )
    from xiaowei_agent.application.runtime import XiaoweiRuntime
    from xiaowei_agent.contracts import (
        AdmissionCertificate,
        RequestContext,
        ToolCall,
        ToolResult,
    )
    from xiaowei_agent.evidence.builder import SlowQueryEvidencePolicy
    from xiaowei_agent.interfaces.feishu_identity import FeishuIdentityDirectory
    from xiaowei_agent.interfaces.feishu_listener import FeishuListener
    from xiaowei_agent.interfaces.feishu_sdk import FeishuInboundTransport
    from xiaowei_agent.interfaces.web_auth import FeishuOAuthPort, WebAuthService
    from xiaowei_agent.persistence.channel import ChannelStore
    from xiaowei_agent.persistence.web_session import WebSessionStore
    from xiaowei_agent.tools.adapter import AdapterResponse, ToolAdapter
    from xiaowei_agent.tools.gateway import TargetBoundAdapterBinding, ToolGateway
    from xiaowei_agent.tools.starrocks import (
        PhysicalIdentityProbe,
        StarRocksConnectionFactory,
    )

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
    model_artifact_store: ModelArtifactStore
    clock: Clock
    monotonic: MonotonicClock
    settings: Settings
    readiness: ReadinessProbe
    aclose: AsyncClose
    policy_revision: str
    intent_model: IntentModelPort | None
    slow_query_advisory: SlowQueryAdvisoryPort | None
    model_profile: ModelInvocationProfile | None


@dataclass(frozen=True)
class TaskViewStack:
    """internal-api 的窄装配；只包含任务读写与终态投影依赖。"""

    runtime: TaskViewRuntime
    task_store: TaskStore
    plan_store: PlanStore
    evidence_ledger: EvidenceLedger
    clock: Clock
    settings: Settings
    readiness: ReadinessProbe
    aclose: AsyncClose
    policy_revision: str


@dataclass(frozen=True)
class FeishuListenerStack:
    """无执行权的飞书入站进程装配。"""

    listener: FeishuListener
    transport: FeishuInboundTransport
    runtime: TaskViewRuntime
    task_store: TaskStore
    channel_store: ChannelStore
    identity_directory: FeishuIdentityDirectory
    submission_service: ChannelSubmissionService
    clock: Clock
    settings: Settings
    readiness: ReadinessProbe
    aclose: AsyncClose
    policy_revision: str


@dataclass(frozen=True)
class ChannelWorkerStack:
    """无执行权的飞书出站投影进程装配。"""

    service: ChannelProjectionService
    message_port: ChannelMessagePort
    runtime: TaskViewRuntime
    task_store: TaskStore
    channel_store: ChannelStore
    clock: Clock
    settings: Settings
    readiness: ReadinessProbe
    aclose: AsyncClose
    policy_revision: str


@dataclass(frozen=True)
class WebStack:
    """无执行权的 Web 认证与任务投影进程装配。"""

    auth: WebAuthService
    oauth_port: FeishuOAuthPort
    membership: FeishuMembershipPort
    runtime: TaskViewRuntime
    task_store: TaskStore
    channel_store: ChannelStore
    web_session_store: WebSessionStore
    identity_directory: FeishuIdentityDirectory
    task_access_service: TaskAccessService
    submission_service: ChannelSubmissionService
    clock: Clock
    settings: Settings
    readiness: ReadinessProbe
    aclose: AsyncClose
    policy_revision: str


class WebStackConfigurationError(RuntimeError):
    """Web 窄栈的 credential 或身份文件配置不可用。"""


@dataclass(frozen=True)
class StarRocksLiveAssembly:
    """代码评审后显式注入的 M6b 物理身份与 Evidence 授权。"""

    identity_probe: PhysicalIdentityProbe
    connection_factory: StarRocksConnectionFactory | None = None
    driver_version: str | None = None
    unredacted_rows_approved: bool = False

    def __post_init__(self) -> None:
        if self.unredacted_rows_approved is not True:
            raise ValueError("M6b live assembly requires explicit row evidence approval")
        if (self.connection_factory is None) != (self.driver_version is None):
            raise ValueError("injected StarRocks factory and driver version must be paired")
        if self.driver_version is not None and (
            not self.driver_version or self.driver_version != self.driver_version.strip()
        ):
            raise ValueError("injected StarRocks driver version must be non-empty")


class _Ready:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True,
            revision_matches_head=True,
            assembled=True,
        )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _starrocks_gateway_registration(
    *,
    settings: Settings,
    live: StarRocksLiveAssembly | None,
    clock: Clock,
    monotonic: MonotonicClock,
) -> tuple[
    dict[str, ToolAdapter],
    dict[tuple[str, str], TargetBoundAdapterBinding],
    SlowQueryEvidencePolicy | None,
]:
    from xiaowei_agent.capabilities.specs import (
        GATEWAY_NAME,
        OP_COUNT,
        OP_LIST,
        SLOW_QUERY_SURFACE,
    )
    from xiaowei_agent.capabilities.target import (
        TargetResolutionError,
        resolve_context_target,
    )
    from xiaowei_agent.contracts import RequestContext
    from xiaowei_agent.evidence.builder import SlowQueryEvidencePolicy
    from xiaowei_agent.planning import compute_target_fingerprint
    from xiaowei_agent.planning.starrocks.compiler import COUNT_V1, LIST_V1
    from xiaowei_agent.tools.gateway import TargetBoundAdapterBinding
    from xiaowei_agent.tools.starrocks import (
        NORMALIZER_VERSION,
        PyMySQLConnectionFactory,
        StarRocksReadonlyAdapter,
        StarRocksReadonlyAdapterConfig,
    )
    from xiaowei_agent.tools.starrocks_fake import StarRocksRecordingAdapter
    from xiaowei_agent.tools.starrocks_recording import default_recording

    if settings.starrocks_adapter_mode == "recording":
        if live is not None:
            raise ValueError("recording mode cannot carry a live StarRocks assembly")
        recording_adapter = StarRocksRecordingAdapter(
            default_recording(list_operation=OP_LIST, count_operation=OP_COUNT)
        )
        return {GATEWAY_NAME: recording_adapter}, {}, None

    if live is None:
        raise ValueError("test_readonly mode requires a code-owned live assembly")
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="0" * 32,
        policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
    )
    try:
        target = resolve_context_target(context=context)
    except TargetResolutionError:
        raise ValueError("StarRocks test target is not uniquely approved") from None
    resource_id = cast(str, settings.starrocks_resource_id)
    if target.resource_ids != (resource_id,):
        raise ValueError("StarRocks resource configuration differs from the target directory")

    factory: StarRocksConnectionFactory
    driver_version: str
    if live.connection_factory is not None:
        factory = live.connection_factory
        driver_version = cast(str, live.driver_version)
    else:
        pymysql_factory = PyMySQLConnectionFactory(
            host=cast(str, settings.starrocks_host),
            port=cast(int, settings.starrocks_port),
            database=cast(str, settings.starrocks_database),
            user=cast(str, settings.starrocks_user),
            tls_mode=cast(str, settings.starrocks_tls_mode),
            ca_file=cast(str, settings.starrocks_ca_file),
            server_name=cast(str, settings.starrocks_server_name),
            connect_timeout_seconds=cast(
                int, settings.starrocks_connect_timeout_seconds
            ),
            read_timeout_seconds=cast(int, settings.starrocks_read_timeout_seconds),
            write_timeout_seconds=cast(int, settings.starrocks_write_timeout_seconds),
        )
        factory = pymysql_factory
        driver_version = pymysql_factory.driver_version

    revision = settings.starrocks_config_revision(
        driver_version=driver_version,
        normalizer_version=NORMALIZER_VERSION,
        sql_surface_ref=SLOW_QUERY_SURFACE.surface_id,
    )
    fingerprint = compute_target_fingerprint(target)
    source_ref = cast(str, settings.starrocks_expected_metadata_source_ref)
    identity_ref = cast(str, settings.starrocks_physical_identity_ref)
    live_adapter = StarRocksReadonlyAdapter(
        config=StarRocksReadonlyAdapterConfig(
            password_file=cast(str, settings.starrocks_password_file),
            query_timeout_seconds=cast(int, settings.starrocks_query_timeout_seconds),
            expected_version_sha256=cast(
                str, settings.starrocks_expected_version_sha256
            ),
            expected_grants_sha256=cast(
                str, settings.starrocks_expected_grants_sha256
            ),
            expected_ddl_sha256=cast(str, settings.starrocks_expected_ddl_sha256),
            expected_identity_sha256=cast(
                str, settings.starrocks_expected_identity_sha256
            ),
            audit_table=SLOW_QUERY_SURFACE.allowed_tables[0],
            list_columns=SLOW_QUERY_SURFACE.allowed_columns,
            identity_probe=live.identity_probe,
            list_operation=OP_LIST,
            count_operation=OP_COUNT,
            list_template_id=LIST_V1,
            count_template_id=COUNT_V1,
        ),
        connection_factory=factory,
        clock=clock,
        monotonic=monotonic,
    )
    binding = TargetBoundAdapterBinding(
        adapter=live_adapter,
        authorized_tenant_id=settings.tenant_id,
        authorized_environment_id=settings.environment_id,
        authorized_actor=cast(str, settings.starrocks_authorized_actor),
        active_from=cast(dt.datetime, settings.starrocks_active_from),
        active_until=cast(dt.datetime, settings.starrocks_active_until),
        evidence_source_ref=source_ref,
        config_revision=revision,
        physical_identity_ref=identity_ref,
        driver_version=driver_version,
    )
    policy = SlowQueryEvidencePolicy(
        approved_target=target,
        target_fingerprint=fingerprint,
        evidence_source_ref=source_ref,
        config_revision=revision,
        physical_identity_ref=identity_ref,
        driver_version=driver_version,
        redaction_ref=None,
    )
    return {}, {(GATEWAY_NAME, fingerprint): binding}, policy


def _build_capability_bindings(
    *, slow_query_live_policy: SlowQueryEvidencePolicy | None = None
) -> tuple[CapabilitySnapshot, CapabilityBindingRegistry]:
    snapshot = StaticCapabilityRegistry().snapshot()
    bindings = build_default_capability_bindings(
        snapshot=snapshot,
        policy_snapshot=ACTIVE_POLICY_SNAPSHOT,
        slow_query_live_policy=slow_query_live_policy,
    )
    return snapshot, bindings


def _assemble_local_stack(
    *,
    settings: Settings,
    task_store: TaskStore,
    channel_store: ChannelStore,
    plan_store: PlanStore,
    ledger: EvidenceLedger,
    model_artifacts: ModelArtifactStore,
    readiness: ReadinessProbe,
    aclose: AsyncClose,
    clock: Clock,
    monotonic: MonotonicClock,
    starrocks_live_assembly: StarRocksLiveAssembly | None,
) -> LocalStack:
    from xiaowei_agent.application.context import ContextAssembler
    from xiaowei_agent.application.runtime import XiaoweiRuntime
    from xiaowei_agent.capabilities.asset_inventory import (
        ASSET_INVENTORY_GATEWAY,
        OP_LOOKUP_ASSET,
    )
    from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
    from xiaowei_agent.capabilities.prometheus_alert import (
        ALERTMANAGER_GATEWAY,
        OP_GET_ACTIVE_ALERTS,
        OP_QUERY_METRIC_RANGE,
        PROMETHEUS_GATEWAY,
        PROMQL_SURFACE,
    )
    from xiaowei_agent.capabilities.resolver_impl import (
        DeterministicCapabilityResolver,
    )
    from xiaowei_agent.governance.approval import NeverGrantingApprovalGate
    from xiaowei_agent.observability.durable_sink import DurableTraceSink
    from xiaowei_agent.planning.prometheus.compiler import compile_promql
    from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
    from xiaowei_agent.planning.prometheus.templates import (
        metric_name_for_template,
        template_for_alert,
    )
    from xiaowei_agent.runners.deterministic import DeterministicStepRunner
    from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
    from xiaowei_agent.tools.alertmanager_recording import (
        default_alertmanager_recording,
    )
    from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingAdapter
    from xiaowei_agent.tools.asset_inventory_recording import (
        default_asset_inventory_recording,
    )
    from xiaowei_agent.tools.gateway import DeterministicToolGateway
    from xiaowei_agent.tools.prometheus_fake import (
        PrometheusRecordingAdapter,
        PrometheusRecordingKey,
    )
    from xiaowei_agent.tools.prometheus_recording import default_prometheus_recording

    application_model_profile = ModelInvocationProfile()
    model_adapter = None
    model_profile = None
    if settings.gemini_enabled:
        from xiaowei_agent.interfaces.gemini_model import (
            GEMINI_MODEL_PROFILE,
            GeminiModelAdapter,
        )

        model_profile = GEMINI_MODEL_PROFILE
        model_adapter = GeminiModelAdapter(profile=model_profile)
        application_model_profile = model_profile

    starrocks_adapters, target_adapters, slow_query_live_policy = (
        _starrocks_gateway_registration(
            settings=settings,
            live=starrocks_live_assembly,
            clock=clock,
            monotonic=monotonic,
        )
    )
    alertmanager_adapter = AlertmanagerRecordingAdapter(
        default_alertmanager_recording(operation=OP_GET_ACTIVE_ALERTS)
    )
    asset_inventory_adapter = AssetInventoryRecordingAdapter(
        default_asset_inventory_recording(), operation=OP_LOOKUP_ASSET
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
            **starrocks_adapters,
            ALERTMANAGER_GATEWAY: alertmanager_adapter,
            PROMETHEUS_GATEWAY: prometheus_adapter,
            ASSET_INVENTORY_GATEWAY: asset_inventory_adapter,
        },
        target_adapters=target_adapters,
        clock=clock,
    )
    gateway: ToolGateway = (
        _PostResultBarrierGateway(base_gateway)
        if settings.smoke_step_barrier
        else base_gateway
    )
    sink = DurableTraceSink(writer=task_store)
    snapshot, bindings = _build_capability_bindings(
        slow_query_live_policy=slow_query_live_policy,
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
    )
    task_projector = TaskViewRuntime(
        task_store=task_store,
        plan_store=plan_store,
        ledger=ledger,
        bindings=bindings,
        model_artifacts=model_artifacts,
        model_profile=application_model_profile,
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
        model_artifacts=model_artifacts,
        model_profile=application_model_profile,
        intent_model=model_adapter,
        slow_query_advisory=model_adapter,
        context_assembler=ContextAssembler(
            task_store=task_store,
            channel_store=channel_store,
            task_projector=task_projector,
        ),
        model_monotonic=monotonic,
        lease_ttl_seconds=settings.lease_ttl_seconds,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
    )

    return LocalStack(
        runtime=runtime,
        task_store=task_store,
        plan_store=plan_store,
        evidence_ledger=ledger,
        model_artifact_store=model_artifacts,
        clock=clock,
        monotonic=monotonic,
        settings=settings,
        readiness=readiness,
        aclose=aclose,
        policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
        intent_model=model_adapter,
        slow_query_advisory=model_adapter,
        model_profile=model_profile,
    )


def build_in_memory_local_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
    monotonic: MonotonicClock = time.monotonic,
    on_close: Callable[[], None] | None = None,
    starrocks_live_assembly: StarRocksLiveAssembly | None = None,
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
    model_artifacts = InMemoryModelArtifactStore(state=state, clock=clock)

    async def close() -> None:
        if on_close is not None:
            on_close()

    return _assemble_local_stack(
        settings=settings,
        task_store=task_store,
        channel_store=InMemoryChannelStore(clock=clock, state=state),
        plan_store=plan_store,
        ledger=ledger,
        model_artifacts=model_artifacts,
        readiness=_Ready(),
        aclose=close,
        clock=clock,
        monotonic=monotonic,
        starrocks_live_assembly=starrocks_live_assembly,
    )


async def build_postgres_task_view_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
) -> TaskViewStack:
    """为 internal-api 装配只读投影栈，不导入 Runner、Gateway 或工具适配器。"""
    engine = create_database_engine(settings)

    async def close() -> None:
        await engine.dispose()

    try:
        task_store = PostgresTaskStore(
            engine=engine,
            clock=clock,
            task_failure_limit=settings.task_failure_limit,
        )
        plan_store = PostgresPlanStore(engine=engine)
        ledger = PostgresEvidenceLedger(engine=engine)
        model_artifacts = PostgresModelArtifactStore(engine=engine, clock=clock)
        _, bindings = _build_capability_bindings()
        return TaskViewStack(
            runtime=TaskViewRuntime(
                task_store=task_store,
                plan_store=plan_store,
                ledger=ledger,
                bindings=bindings,
                model_artifacts=model_artifacts,
                model_profile=ModelInvocationProfile(),
            ),
            task_store=task_store,
            plan_store=plan_store,
            evidence_ledger=ledger,
            clock=clock,
            settings=settings,
            readiness=PostgresReadinessProbe(engine=engine, assembled=True),
            aclose=close,
            policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
        )
    except Exception:
        await engine.dispose()
        raise


async def build_postgres_feishu_listener_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
    transport: FeishuInboundTransport | None = None,
) -> FeishuListenerStack:
    """装配默认关闭的飞书入口；不创建 Runner、Gateway 或目标 adapter。"""
    from xiaowei_agent.application.channel_submission import ChannelSubmissionService
    from xiaowei_agent.interfaces.feishu_identity import (
        load_feishu_identity_directory,
    )
    from xiaowei_agent.interfaces.feishu_listener import FeishuListener
    from xiaowei_agent.interfaces.feishu_sdk import FeishuSdkInboundTransport

    if not settings.feishu_listener_enabled:
        raise ValueError("Feishu listener is disabled")
    engine = create_database_engine(settings)

    async def close() -> None:
        await engine.dispose()

    try:
        task_store = PostgresTaskStore(
            engine=engine,
            clock=clock,
            task_failure_limit=settings.task_failure_limit,
        )
        plan_store = PostgresPlanStore(engine=engine)
        ledger = PostgresEvidenceLedger(engine=engine)
        model_artifacts = PostgresModelArtifactStore(engine=engine, clock=clock)
        channel_store = PostgresChannelStore(engine=engine, clock=clock)
        _, bindings = _build_capability_bindings()
        runtime = TaskViewRuntime(
            task_store=task_store,
            plan_store=plan_store,
            ledger=ledger,
            bindings=bindings,
            model_artifacts=model_artifacts,
            model_profile=ModelInvocationProfile(),
        )
        identity_directory = load_feishu_identity_directory(
            path=cast(str, settings.feishu_identity_file),
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
        )
        submission_service = ChannelSubmissionService(
            runtime=runtime,
            channel_store=channel_store,
        )
        inbound = (
            transport
            if transport is not None
            else FeishuSdkInboundTransport(
                app_id=cast(str, settings.feishu_app_id),
                app_secret_file=cast(str, settings.feishu_app_secret_file),
            )
        )
        listener = FeishuListener(
            app_id=cast(str, settings.feishu_app_id),
            tenant_key=cast(str, settings.feishu_tenant_key),
            bot_open_id=cast(str, settings.feishu_bot_open_id),
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
            identity_directory=identity_directory,
            submission_service=submission_service,
            policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
            clock=clock,
        )
        return FeishuListenerStack(
            listener=listener,
            transport=inbound,
            runtime=runtime,
            task_store=task_store,
            channel_store=channel_store,
            identity_directory=identity_directory,
            submission_service=submission_service,
            clock=clock,
            settings=settings,
            readiness=PostgresReadinessProbe(engine=engine, assembled=True),
            aclose=close,
            policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
        )
    except Exception:
        await engine.dispose()
        raise


async def build_postgres_channel_worker_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
    message_port: ChannelMessagePort | None = None,
    sleep: AsyncSleep = asyncio.sleep,
) -> ChannelWorkerStack:
    """装配默认关闭的渠道投影；不创建 Runner、Gateway 或目标 adapter。"""
    from xiaowei_agent.application.channel_projection import ChannelProjectionService
    from xiaowei_agent.interfaces.feishu_sdk import FeishuSdkMessageAdapter

    if not settings.channel_worker_enabled:
        raise ValueError("channel worker is disabled")
    engine = create_database_engine(settings)

    async def close() -> None:
        await engine.dispose()

    try:
        task_store = PostgresTaskStore(
            engine=engine,
            clock=clock,
            task_failure_limit=settings.task_failure_limit,
        )
        plan_store = PostgresPlanStore(engine=engine)
        ledger = PostgresEvidenceLedger(engine=engine)
        model_artifacts = PostgresModelArtifactStore(engine=engine, clock=clock)
        channel_store = PostgresChannelStore(engine=engine, clock=clock)
        _, bindings = _build_capability_bindings()
        runtime = TaskViewRuntime(
            task_store=task_store,
            plan_store=plan_store,
            ledger=ledger,
            bindings=bindings,
            model_artifacts=model_artifacts,
            model_profile=ModelInvocationProfile(),
        )
        messages = (
            message_port
            if message_port is not None
            else FeishuSdkMessageAdapter(
                app_id=cast(str, settings.feishu_app_id),
                app_secret_file=cast(str, settings.feishu_app_secret_file),
                timeout_seconds=settings.feishu_api_timeout_seconds,
            )
        )
        service = ChannelProjectionService(
            runtime=runtime,
            task_store=task_store,
            channel_store=channel_store,
            message_port=messages,
            clock=clock,
            settings=settings,
            sleep=sleep,
        )
        return ChannelWorkerStack(
            service=service,
            message_port=messages,
            runtime=runtime,
            task_store=task_store,
            channel_store=channel_store,
            clock=clock,
            settings=settings,
            readiness=PostgresReadinessProbe(engine=engine, assembled=True),
            aclose=close,
            policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
        )
    except Exception:
        await engine.dispose()
        raise


async def build_postgres_web_stack(
    *,
    settings: Settings,
    oauth: FeishuOAuthPort,
    membership: FeishuMembershipPort,
    clock: Clock = _utc_now,
) -> WebStack:
    """用注入端口装配 Web 窄栈；不创建 Runner、Gateway 或真实 OAuth 客户端。"""
    from xiaowei_agent.application.channel_access import TaskAccessService
    from xiaowei_agent.application.channel_submission import ChannelSubmissionService
    from xiaowei_agent.interfaces.feishu_identity import (
        FeishuIdentityConfigurationError,
        load_feishu_identity_directory,
    )
    from xiaowei_agent.interfaces.web_auth import (
        FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS,
        WebAuthService,
    )

    if not (settings.web_app_enabled and settings.feishu_oauth_enabled):
        raise ValueError("Web app is disabled")
    engine = None
    database_invalid = False
    try:
        engine = create_database_engine(settings)
    except DatabaseConfigurationError:
        database_invalid = True
    if database_invalid or engine is None:
        raise WebStackConfigurationError("web stack configuration invalid")

    async def close() -> None:
        await engine.dispose()

    try:
        task_store = PostgresTaskStore(
            engine=engine,
            clock=clock,
            task_failure_limit=settings.task_failure_limit,
        )
        plan_store = PostgresPlanStore(engine=engine)
        ledger = PostgresEvidenceLedger(engine=engine)
        model_artifacts = PostgresModelArtifactStore(engine=engine, clock=clock)
        channel_store = PostgresChannelStore(engine=engine, clock=clock)
        web_session_store = PostgresWebSessionStore(engine=engine, clock=clock)
        _, bindings = _build_capability_bindings()
        runtime = TaskViewRuntime(
            task_store=task_store,
            plan_store=plan_store,
            ledger=ledger,
            bindings=bindings,
            model_artifacts=model_artifacts,
            model_profile=ModelInvocationProfile(),
        )
        identity_directory = load_feishu_identity_directory(
            path=cast(str, settings.feishu_identity_file),
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
        )
        task_access_service = TaskAccessService(
            runtime=runtime,
            task_store=task_store,
            channel_store=channel_store,
            membership=membership,
        )
        submission_service = ChannelSubmissionService(
            runtime=runtime,
            channel_store=channel_store,
            web_parent_access=task_access_service,
        )
        auth = WebAuthService(
            sessions=web_session_store,
            identities=identity_directory,
            oauth=oauth,
            public_origin=cast(str, settings.web_public_origin),
            oauth_state_ttl_seconds=settings.web_oauth_state_ttl_seconds,
            session_ttl_seconds=settings.web_session_ttl_seconds,
            oauth_timeout_seconds=FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS,
        )
        return WebStack(
            auth=auth,
            oauth_port=oauth,
            membership=membership,
            runtime=runtime,
            task_store=task_store,
            channel_store=channel_store,
            web_session_store=web_session_store,
            identity_directory=identity_directory,
            task_access_service=task_access_service,
            submission_service=submission_service,
            clock=clock,
            settings=settings,
            readiness=PostgresReadinessProbe(engine=engine, assembled=True),
            aclose=close,
            policy_revision=ACTIVE_POLICY_SNAPSHOT.policy_revision,
        )
    except FeishuIdentityConfigurationError:
        await engine.dispose()
    except Exception:
        await engine.dispose()
        raise
    raise WebStackConfigurationError("web stack configuration invalid")


async def build_postgres_local_stack(
    *,
    settings: Settings,
    clock: Clock = _utc_now,
    monotonic: MonotonicClock = time.monotonic,
    starrocks_live_assembly: StarRocksLiveAssembly | None = None,
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
            channel_store=PostgresChannelStore(engine=engine, clock=clock),
            plan_store=PostgresPlanStore(engine=engine),
            ledger=PostgresEvidenceLedger(engine=engine),
            model_artifacts=PostgresModelArtifactStore(engine=engine, clock=clock),
            readiness=PostgresReadinessProbe(engine=engine, assembled=True),
            aclose=close,
            clock=clock,
            monotonic=monotonic,
            starrocks_live_assembly=starrocks_live_assembly,
        )
    except Exception:
        await engine.dispose()
        raise

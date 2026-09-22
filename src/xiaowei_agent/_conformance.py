"""Protocol 一致性锚点。

**为什么必须放在 ``src`` 而不是 ``tests``**：验收命令是 ``mypy src``，放在
``tests/`` 下的带类型标注赋值根本不会被检查；而 pytest 运行它只是在运行时断言
实例存在，证明不了 structural typing 没有漂移。Protocol 只在**有赋值发生时**才被
静态检查，因此需要一处显式赋值作为锚。

全部内容位于 ``TYPE_CHECKING`` 块内，运行时零开销、零导入副作用——特别是不会把
fake 实现拉进生产导入链（``tests/security/test_fake_isolation.py`` 断言这一点，
故此处的 fake 导入同样只在类型检查期存在）。

M3 补齐了五个此前只有形状、没有实现的 Protocol 锚点：``CapabilityRegistry``、
``CapabilityResolver``、``TraceSink``、``PlanStore``、``EvidenceLedger``。

``WorkflowRunner`` 锚定**两个**实现：fake 的 ``ScriptedRunner`` 与真实的
``DeterministicStepRunner``。只锚 fake 是不够的——真实 Runner 才是生产调用路径，
它一旦偏离契约，调用方要么改签名要么绕过 Protocol，而后者不会有任何静态检查失败。
"""

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - 仅供 mypy 检查结构兼容性
    from tests.fakes.model import ScriptedModelAdapter

    from xiaowei_agent.application.capability_input import CapabilityPlanner
    from xiaowei_agent.application.capability_runtime import (
        CapabilityAdvisoryProjector,
        CapabilityAssessor,
        CapabilityBindingRegistry,
        CapabilityRenderer,
    )
    from xiaowei_agent.application.channel_access import FeishuMembershipPort
    from xiaowei_agent.application.channel_projection import (
        ChannelMessagePort,
        TaskProjectionPort,
    )
    from xiaowei_agent.application.default_capabilities import SLOW_QUERY_BINDING
    from xiaowei_agent.application.model_ports import (
        InteractionClassifierPort,
        SlowQueryAdvisoryPort,
    )
    from xiaowei_agent.application.task_heartbeat import LeaseRenewalPort
    from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
    from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
    from xiaowei_agent.capabilities.resolver import (
        CapabilityRegistry,
        CapabilityResolver,
    )
    from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
    from xiaowei_agent.contracts import TaskStatus
    from xiaowei_agent.interfaces.feishu_identity import (
        FeishuIdentityDirectory,
        StaticFeishuIdentityDirectory,
    )
    from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter
    from xiaowei_agent.interfaces.feishu_sdk import (
        FeishuInboundTransport,
        FeishuSdkInboundTransport,
        FeishuSdkMembershipAdapter,
        FeishuSdkMessageAdapter,
    )
    from xiaowei_agent.interfaces.gemini_model import GeminiModelAdapter
    from xiaowei_agent.interfaces.web_auth import FeishuOAuthPort
    from xiaowei_agent.observability.log_sink import StructuredLogTraceSink
    from xiaowei_agent.observability.sink import TraceSink
    from xiaowei_agent.persistence.admin_audit import AdminAuditStore
    from xiaowei_agent.persistence.channel import ChannelStore
    from xiaowei_agent.persistence.clarification_records import (
        ClarificationRecordStore,
        InMemoryClarificationRecordStore,
    )
    from xiaowei_agent.persistence.evidence import EvidenceLedger, InMemoryEvidenceLedger
    from xiaowei_agent.persistence.fake import (
        InMemoryAdminAuditStore,
        InMemoryChannelStore,
        InMemoryTaskStore,
        InMemoryUserDirectoryStore,
        InMemoryWebSessionStore,
    )
    from xiaowei_agent.persistence.identity import UserDirectoryStore
    from xiaowei_agent.persistence.model_artifacts import (
        InMemoryModelArtifactStore,
        ModelArtifactStore,
    )
    from xiaowei_agent.persistence.plans import InMemoryPlanStore, PlanStore
    from xiaowei_agent.persistence.postgres import (
        PostgresAdminAuditStore,
        PostgresChannelStore,
        PostgresClarificationRecordStore,
        PostgresEvidenceLedger,
        PostgresModelArtifactStore,
        PostgresPlanStore,
        PostgresTaskStore,
        PostgresUserDirectoryStore,
        PostgresWebSessionStore,
    )
    from xiaowei_agent.persistence.store import Clock, TaskStore
    from xiaowei_agent.persistence.web_session import WebSessionStore
    from xiaowei_agent.planning.starrocks.params import SlowQueryParams
    from xiaowei_agent.runners.binding import (
        ExecutionBindingProvider,
        StepEvidenceBuilder,
    )
    from xiaowei_agent.runners.deterministic import DeterministicStepRunner
    from xiaowei_agent.runners.fake import ScriptedRunner
    from xiaowei_agent.runners.runner import WorkflowRunner
    from xiaowei_agent.tools.adapter import AdapterResponse, ToolAdapter
    from xiaowei_agent.tools.fake import RecordingToolAdapter
    from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway

    def _anchor(
        clock: "Clock",
        responses: tuple["AdapterResponse", ...],
    ) -> None:
        adapter: ToolAdapter = RecordingToolAdapter(responses=responses)
        gateway: ToolGateway = DeterministicToolGateway(adapters={"starrocks": adapter})
        store: TaskStore = InMemoryTaskStore(clock=clock)
        channels: ChannelStore = InMemoryChannelStore(clock=clock)
        sessions: WebSessionStore = InMemoryWebSessionStore(clock=clock)
        runner: WorkflowRunner = ScriptedRunner(
            store, outcome_status=TaskStatus.SUCCEEDED
        )
        registry: CapabilityRegistry = StaticCapabilityRegistry()
        resolver: CapabilityResolver = DeterministicCapabilityResolver()
        sink: TraceSink = StructuredLogTraceSink()
        plans: PlanStore = InMemoryPlanStore()
        ledger: EvidenceLedger = InMemoryEvidenceLedger()
        _ = (
            gateway,
            runner,
            registry,
            resolver,
            sink,
            plans,
            ledger,
            channels,
            sessions,
        )

    def _postgres_store_anchor(store: "PostgresTaskStore") -> None:
        """PostgreSQL 实现必须**就是**一个 ``TaskStore``。

        与 ``_real_runner_anchor`` 同一理由：只锚 fake 不够。取参数而不在此构造，
        因为构造它需要一个 ``AsyncEngine``，而结构兼容性只需要一次赋值。
        """
        anchored: TaskStore = store
        _ = anchored

    def _model_artifact_store_anchors(
        memory: "InMemoryModelArtifactStore",
        postgres: "PostgresModelArtifactStore",
    ) -> None:
        """两种模型事实存储都必须保持同一 grant-fenced 窄协议。"""
        memory_port: ModelArtifactStore = memory
        postgres_port: ModelArtifactStore = postgres
        _ = (memory_port, postgres_port)

    def _clarification_record_store_anchors(
        memory: "InMemoryClarificationRecordStore",
        postgres: "PostgresClarificationRecordStore",
    ) -> None:
        """两种澄清事实存储都必须保持同一 grant-fenced 窄协议。"""
        memory_port: ClarificationRecordStore = memory
        postgres_port: ClarificationRecordStore = postgres
        _ = (memory_port, postgres_port)

    def _identity_directory_anchors(
        memory: "InMemoryUserDirectoryStore",
        postgres: "PostgresUserDirectoryStore",
    ) -> None:
        """两种身份目录实现都必须保持同一条**单写入口**协议。

        只锚一个实现时，另一个多长出一个公开写方法不会有任何静态检查失败——而多
        出来的那个正是绕过"授权改变必带同事务审计"的唯一途径。
        """
        memory_port: UserDirectoryStore = memory
        postgres_port: UserDirectoryStore = postgres
        _ = (memory_port, postgres_port)

    def _admin_audit_store_anchors(
        memory: "InMemoryAdminAuditStore",
        postgres: "PostgresAdminAuditStore",
    ) -> None:
        """两种审计实现都必须保持同一套 append-only 窄写协议。"""
        memory_port: AdminAuditStore = memory
        postgres_port: AdminAuditStore = postgres
        _ = (memory_port, postgres_port)

    def _lease_renewal_anchor(store: "PostgresTaskStore") -> None:
        """入口 heartbeat 只能要求 TaskStore 的续租窄面。"""
        renewal: LeaseRenewalPort = store
        _ = renewal

    def _postgres_channel_store_anchor(store: "PostgresChannelStore") -> None:
        """PostgreSQL 渠道实现必须保持与内存实现相同的 ``ChannelStore`` 端口。"""
        anchored: ChannelStore = store
        _ = anchored

    def _postgres_web_session_store_anchor(
        store: "PostgresWebSessionStore",
    ) -> None:
        """PostgreSQL Web session 实现必须保持 digest-only 原子存储端口。"""
        anchored: WebSessionStore = store
        _ = anchored

    def _feishu_port_anchors(
        identity: "StaticFeishuIdentityDirectory",
        inbound: "FeishuSdkInboundTransport",
        membership: "FeishuSdkMembershipAdapter",
        messages: "FeishuSdkMessageAdapter",
        oauth: "FeishuOAuthAdapter",
    ) -> None:
        """SDK、身份与 OAuth 实现必须保持应用层和入口层的本地窄协议。"""
        identity_port: FeishuIdentityDirectory = identity
        inbound_port: FeishuInboundTransport = inbound
        membership_port: FeishuMembershipPort = membership
        message_port: ChannelMessagePort = messages
        oauth_port: FeishuOAuthPort = oauth
        _ = (identity_port, inbound_port, membership_port, message_port, oauth_port)

    def _task_projection_anchor(runtime: "TaskViewRuntime") -> None:
        """真实读取 Runtime 必须满足渠道只读投影的最小端口。"""
        anchored: TaskProjectionPort = runtime
        _ = anchored

    def _model_port_anchors(
        gemini: "GeminiModelAdapter",
        scripted: "ScriptedModelAdapter",
    ) -> None:
        """两个模型 port 的真实/fake 实现都必须保持窄签名。"""
        real_interaction: InteractionClassifierPort = gemini
        real_advisory: SlowQueryAdvisoryPort = gemini
        fake_interaction: InteractionClassifierPort = scripted
        fake_advisory: SlowQueryAdvisoryPort = scripted
        _ = (real_interaction, real_advisory, fake_interaction, fake_advisory)

    def _postgres_port_anchors(
        plans: "PostgresPlanStore", ledger: "PostgresEvidenceLedger"
    ) -> None:
        """T7 实现的是**既有 port**，不是新 port——两条赋值把这一点交给 mypy 检查。"""
        anchored_plans: PlanStore = plans
        anchored_ledger: EvidenceLedger = ledger
        _ = (anchored_plans, anchored_ledger)

    def _real_runner_anchor(step_runner: "DeterministicStepRunner") -> None:
        """真实 Runner 必须**就是**一个 ``WorkflowRunner``。

        取参数而不在此构造：``DeterministicStepRunner`` 需要八个协作者，构造它会把
        一堆无关装配拖进锚点文件；而结构兼容性只需要一次赋值就能被 mypy 检查。
        """
        anchored: WorkflowRunner = step_runner
        _ = anchored

    def _capability_binding_anchors(
        registry: "CapabilityBindingRegistry",
    ) -> None:
        """显式 binding 实现必须保持 application 与 Runner 两侧的窄协议。"""
        provider: ExecutionBindingProvider = registry
        planner: CapabilityPlanner[SlowQueryParams] = (
            SLOW_QUERY_BINDING.input_binding.planner
        )
        assessor: CapabilityAssessor = SLOW_QUERY_BINDING.assessor
        renderer: CapabilityRenderer = SLOW_QUERY_BINDING.renderer
        advisory_projector: CapabilityAdvisoryProjector = (
            cast(CapabilityAdvisoryProjector, SLOW_QUERY_BINDING.advisory_projector)
        )
        evidence_builder: StepEvidenceBuilder = (
            SLOW_QUERY_BINDING.execution.evidence_builder
        )
        _ = (
            provider,
            planner,
            assessor,
            renderer,
            advisory_projector,
            evidence_builder,
        )

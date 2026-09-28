"""单进程持久化适配器共享的状态容器。"""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xiaowei_agent.contracts import (
    ApprovalRequest,
    EvidenceEnvelope,
    TaskRecord,
    TaskSubmission,
    TraceEvent,
)

if TYPE_CHECKING:
    from xiaowei_agent.contracts import LoadReceipt, ReceiptKey, TestResult
    from xiaowei_agent.contracts.activation import ActivationRequest
    from xiaowei_agent.contracts.admin_audit import AdminAuditEvent
    from xiaowei_agent.contracts.identity import (
        ExternalIdentity,
        UserAccount,
        UserRoleAssignment,
    )
    from xiaowei_agent.contracts.web_navigation import WebReturnIntent
    from xiaowei_agent.persistence.admin_audit import AuditStage
    from xiaowei_agent.persistence.channel import ChannelBinding, ProjectionSubscription
    from xiaowei_agent.persistence.store import SqlArtifactRecord
    from xiaowei_agent.persistence.web_session import OAuthState, WebSession


@dataclass(frozen=True, slots=True)
class OAuthTestContext:
    """内存侧的测试 context 行：与 ``web_oauth_test_contexts`` 两列一一对应。"""

    operation_id: str
    config_generation: int


class InMemoryPersistenceState:
    """让单进程持久化端口共享同一锁和事实命名空间。"""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: dict[str, TaskRecord] = {}
        # 对话键为 (tenant, environment, key)；SQL 键在末尾加 input_kind，两者分属不同作用域。
        self.task_ids_by_key: dict[tuple[str, ...], str] = {}
        self.sql_artifacts: dict[str, SqlArtifactRecord] = {}
        self.submissions: dict[str, TaskSubmission] = {}
        self.submission_digests: dict[str, str] = {}
        self.plans: dict[str, Any] = {}
        self.evidence: dict[str, dict[str, EvidenceEnvelope]] = {}
        self.interaction_artifacts: dict[str, Any] = {}
        self.model_advisories: dict[str, Any] = {}
        self.clarification_records: dict[str, Any] = {}
        self.approvals: dict[str, list[ApprovalRequest]] = {}
        self.audit_events: dict[str, list[TraceEvent]] = {}
        self.step_executions: dict[tuple[str, str], Any] = {}
        self.channel_bindings: dict[str, ChannelBinding] = {}
        self.channel_binding_ids_by_source: dict[tuple[str, str, str, str], str] = {}
        self.channel_binding_ids_by_task: dict[str, str] = {}
        # 来源事件 → 选定的提交类型（ADR-018 D2 渠道侧约束）；键与绑定的来源唯一键相同。
        self.channel_source_kinds: dict[tuple[str, str, str, str], str] = {}
        self.projection_subscriptions: dict[str, ProjectionSubscription] = {}
        self.projection_subscription_ids_by_destination: dict[
            tuple[str, str, str], str
        ] = {}
        self.oauth_states: dict[str, OAuthState] = {}
        self.oauth_login_contexts: dict[str, WebReturnIntent] = {}
        # W4a：测试 state → 审计 operation id 与被测代次；与登录 context 同样随 state 收割。
        self.oauth_test_contexts: dict[str, OAuthTestContext] = {}
        self.web_sessions: dict[str, WebSession] = {}
        self.local_admin: Any | None = None
        self.user_accounts: dict[str, UserAccount] = {}
        self.user_role_assignments: dict[tuple[str, str, str], UserRoleAssignment] = {}
        # 键与 ``external_identities`` 的主键**逐列相同**。键形状不一致时，共享
        # 套件会在两个实现上测出不同的冲突语义，而套件正是为了排除这种分叉。
        self.external_identities: dict[
            tuple[str, str, str, str], ExternalIdentity
        ] = {}
        self.admin_audit_events: dict[str, AdminAuditEvent] = {}
        # 两条偏唯一索引在内存里的对应物：一次操作每个阶段最多一条事件。
        self.admin_audit_stage_keys: dict[tuple[str, AuditStage], str] = {}
        self.activation_requests: dict[str, ActivationRequest] = {}
        self.load_receipts: dict[ReceiptKey, LoadReceipt] = {}
        self.provider_tests: dict[str, TestResult] = {}
        self.next_fencing_token = 1
        self.next_projection_fencing_token = 1
        self.next_created_seq = 1

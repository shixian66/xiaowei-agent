"""单进程持久化适配器共享的状态容器。"""

import asyncio
from typing import TYPE_CHECKING, Any

from xiaowei_agent.contracts import (
    ApprovalRequest,
    EvidenceEnvelope,
    TaskRecord,
    TaskSubmission,
    TraceEvent,
)

if TYPE_CHECKING:
    from xiaowei_agent.contracts import LoadReceipt, TestResult
    from xiaowei_agent.contracts.admin_audit import AdminAuditEvent
    from xiaowei_agent.contracts.identity import (
        ExternalIdentity,
        UserAccount,
        UserRoleAssignment,
    )
    from xiaowei_agent.persistence.admin_audit import AuditStage
    from xiaowei_agent.persistence.channel import ChannelBinding, ProjectionSubscription
    from xiaowei_agent.persistence.web_session import OAuthState, WebSession


class InMemoryPersistenceState:
    """让单进程持久化端口共享同一锁和事实命名空间。"""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: dict[str, TaskRecord] = {}
        self.task_ids_by_key: dict[tuple[str, str, str], str] = {}
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
        self.projection_subscriptions: dict[str, ProjectionSubscription] = {}
        self.projection_subscription_ids_by_destination: dict[
            tuple[str, str, str], str
        ] = {}
        self.oauth_states: dict[str, OAuthState] = {}
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
        self.load_receipts: dict[tuple[str, str], LoadReceipt] = {}
        self.provider_tests: dict[str, TestResult] = {}
        self.next_fencing_token = 1
        self.next_projection_fencing_token = 1
        self.next_created_seq = 1

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
        self.load_receipts: dict[tuple[str, str], LoadReceipt] = {}
        self.provider_tests: dict[str, TestResult] = {}
        self.next_fencing_token = 1
        self.next_projection_fencing_token = 1
        self.next_created_seq = 1

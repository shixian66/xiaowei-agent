"""单进程持久化适配器共享的状态容器。"""

import asyncio
from typing import Any

from xiaowei_agent.contracts import (
    ApprovalRequest,
    EvidenceEnvelope,
    TaskRecord,
    TaskSubmission,
    TraceEvent,
)


class InMemoryPersistenceState:
    """让 TaskStore、PlanStore 与 EvidenceLedger 共享同一锁和事实命名空间。"""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: dict[str, TaskRecord] = {}
        self.task_ids_by_key: dict[tuple[str, str, str], str] = {}
        self.submissions: dict[str, TaskSubmission] = {}
        self.plans: dict[str, Any] = {}
        self.evidence: dict[str, dict[str, EvidenceEnvelope]] = {}
        self.approvals: dict[str, list[ApprovalRequest]] = {}
        self.audit_events: dict[str, list[TraceEvent]] = {}
        self.next_fencing_token = 1
        self.next_created_seq = 1

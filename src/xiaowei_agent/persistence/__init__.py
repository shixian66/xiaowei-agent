"""任务持久化。M2 只有交互形状与单进程实现；PostgreSQL 属 M4。"""

from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.store import (
    Clock,
    ContextMismatchError,
    IdempotencyConflictError,
    LeaseCommand,
    StaleLeaseQuery,
    TaskNotFoundError,
    TaskStore,
    TransitionCommand,
    UnscopedAuditEventError,
    idempotency_scope_digest,
    request_dedup_digest,
)

__all__ = [
    "Clock",
    "ContextMismatchError",
    "IdempotencyConflictError",
    "LeaseCommand",
    "PersistenceIntegrityCategory",
    "PersistenceIntegrityError",
    "PersistenceUnavailableCategory",
    "PersistenceUnavailableError",
    "PersistenceWriteOutcome",
    "StaleLeaseQuery",
    "TaskNotFoundError",
    "TaskStore",
    "TransitionCommand",
    "UnscopedAuditEventError",
    "idempotency_scope_digest",
    "request_dedup_digest",
]

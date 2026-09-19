"""Grant-fenced, insert-once 澄清事实存储契约与内存实现。"""

from __future__ import annotations

from typing import Final, Protocol, cast

from pydantic import model_validator

from xiaowei_agent.contracts import (
    AwareDatetime,
    ClarificationContext,
    ClarificationReasonCode,
    ClarificationRecord,
    ClarificationSubject,
    ConfirmedSlot,
    Contract,
    GrantRejection,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.contracts.enums import ClarificationField
from xiaowei_agent.persistence.decisions import grant_is_current
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import Clock, TaskAttemptGrant, TaskIdCarryingError


class ClarificationRecordError(TaskIdCarryingError):
    """澄清事实存储错误；task id 仅在结构化属性中。"""


class ClarificationRecordConflictError(ClarificationRecordError):
    """同一 task 已有不同的 insert-once 澄清事实。"""


class ClarificationRecordGrantError(ClarificationRecordError):
    """保存者不再持有当前 task grant。"""


class ClarificationRecordStateError(ClarificationRecordError):
    """任务不存在、已终态或当前状态不允许写入澄清事实。"""


_GRANT_LOSS_REJECTIONS: Final[frozenset[GrantRejection]] = frozenset(
    {GrantRejection.LEASE_NOT_HELD, GrantRejection.STALE_FENCING}
)


def require_clarification_record_grant(
    current: TaskRecord | None,
    grant: TaskAttemptGrant,
    *,
    now: AwareDatetime,
) -> None:
    """共享内存/PostgreSQL 的澄清事实写入拒绝分类。"""
    if current is None:
        raise ClarificationRecordStateError(
            "clarification task does not exist", task_id=grant.task_id
        )
    rejection = grant_is_current(
        current,
        grant,
        now=now,
        allowed_statuses=frozenset(
            {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}
        ),
    )
    if rejection is None:
        return
    if rejection in _GRANT_LOSS_REJECTIONS:
        raise ClarificationRecordGrantError(
            "clarification grant is not current", task_id=grant.task_id
        )
    raise ClarificationRecordStateError(
        "task status does not allow this clarification record", task_id=grant.task_id
    )


class ClarificationRecordCandidate(Contract):
    """应用层已复验、尚未由存储层盖时间与 fencing 的澄清事实。"""

    subject: ClarificationSubject
    reason_code: ClarificationReasonCode
    missing_fields: tuple[ClarificationField, ...]
    confirmed_slots: tuple[ConfirmedSlot, ...]

    @model_validator(mode="after")
    def _snapshot_is_canonical(self) -> ClarificationRecordCandidate:
        ClarificationContext(
            subject=self.subject,
            confirmed_slots=self.confirmed_slots,
        )
        return self


class ClarificationRecordStore(Protocol):
    async def load(self, *, task_id: str) -> ClarificationRecord | None: ...

    async def save(
        self, *, grant: TaskAttemptGrant, candidate: ClarificationRecordCandidate
    ) -> ClarificationRecord: ...


def record_matches_candidate(
    stored: ClarificationRecord, candidate: ClarificationRecordCandidate
) -> bool:
    return all(
        getattr(stored, name) == getattr(candidate, name)
        for name in type(candidate).model_fields
    )


class InMemoryClarificationRecordStore:
    """与 TaskStore 共用锁和任务事实的单进程实现。"""

    def __init__(self, *, state: InMemoryPersistenceState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    async def load(self, *, task_id: str) -> ClarificationRecord | None:
        async with self._state.lock:
            return cast(
                ClarificationRecord | None,
                self._state.clarification_records.get(task_id),
            )

    async def save(
        self, *, grant: TaskAttemptGrant, candidate: ClarificationRecordCandidate
    ) -> ClarificationRecord:
        async with self._state.lock:
            current = self._state.tasks.get(grant.task_id)
            require_clarification_record_grant(
                current,
                grant,
                now=self._clock(),
            )
            existing = self._state.clarification_records.get(grant.task_id)
            if existing is not None:
                stored = cast(ClarificationRecord, existing)
                if not record_matches_candidate(stored, candidate):
                    raise ClarificationRecordConflictError(
                        "different clarification record is already stored",
                        task_id=grant.task_id,
                    )
                return stored
            stored = ClarificationRecord(
                **candidate.model_dump(mode="python"),
                task_id=grant.task_id,
                created_at=self._clock(),
                fencing_token=grant.fencing_token,
            )
            self._state.clarification_records[grant.task_id] = stored
            return stored


__all__ = [
    "ClarificationRecordCandidate",
    "ClarificationRecordConflictError",
    "ClarificationRecordError",
    "ClarificationRecordGrantError",
    "ClarificationRecordStateError",
    "ClarificationRecordStore",
    "InMemoryClarificationRecordStore",
    "record_matches_candidate",
    "require_clarification_record_grant",
]

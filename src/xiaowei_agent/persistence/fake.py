"""单进程 TaskStore 实现（DEVELOPMENT_PLAN §7 M2 明文要求置于 persistence/）。

**真实执行**：幂等作用域、expected-version 检查、返回存储层 winner、lease/fencing
闭合规则、终态保护、迁移表闭集。
**不宣称**：跨进程原子性、崩溃恢复、PostgreSQL 级隔离——这些属于 M4。

用一把 ``asyncio.Lock`` 串行化全部写入，使单进程内的 CAS 语义确定；这不等于分布式
原子性，测试与文档都不这样表述。

**拒绝顺序是有语义的**：终态保护 → 租约/fencing → 版本 → 迁移合法性。终态放最前，
使"违反终态保护"永远以 ``TERMINAL_PROTECTED`` 报出，不会被版本不匹配掩盖成一个
看起来正常的并发结果；租约放在版本之前，是因为"拿着陈旧 token 的 worker"比"版本
落后"更具体，审计需要看到前者。
"""

import asyncio
import datetime as _dt
import uuid
from typing import Final

from xiaowei_agent.contracts import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ApprovalRequest,
    LeaseGrant,
    RequestContext,
    RequestEnvelope,
    TaskRecord,
    TaskStatus,
    TransitionRejection,
    TransitionResult,
)
from xiaowei_agent.persistence.store import (
    Clock,
    ContextMismatchError,
    IdempotencyConflictError,
    TaskNotFoundError,
    request_dedup_digest,
)

IS_FAKE: Final[bool] = True
"""供 tests/security/test_fake_isolation.py 断言；生产模块不得导入本模块。"""


class InMemoryTaskStore:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._records: dict[str, TaskRecord] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        self._approvals: list[ApprovalRequest] = []
        self._next_token = 1
        self._lock = asyncio.Lock()

    def _require(self, task_id: str) -> TaskRecord:
        try:
            return self._records[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id) from exc

    def _lease_is_live(self, record: TaskRecord) -> bool:
        return (
            record.lease_owner is not None
            and record.lease_expires_at is not None
            and record.lease_expires_at > self._clock()
        )

    async def create_task(
        self, *, envelope: RequestEnvelope, context: RequestContext
    ) -> TaskRecord:
        # 先校验上下文一致性，再谈幂等：不一致时连"属于哪个作用域"都不成立。
        if (
            envelope.tenant_id != context.tenant_id
            or envelope.actor != context.actor
            or (
                envelope.environment_id is not None
                and envelope.environment_id != context.environment_id
            )
        ):
            raise ContextMismatchError("envelope and context disagree on execution context")
        digest = request_dedup_digest(envelope, context)
        scope = (context.tenant_id, context.environment_id, envelope.idempotency_key)
        async with self._lock:
            existing_id = self._by_key.get(scope)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.request_digest != digest:
                    raise IdempotencyConflictError(
                        "idempotency key reused for a different request"
                    )
                return existing
            record = TaskRecord(
                task_id=str(uuid.uuid4()),
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
                actor=context.actor,
                idempotency_key=envelope.idempotency_key,
                request_digest=digest,
                status=TaskStatus.CREATED,
                version=0,
            )
            self._records[record.task_id] = record
            self._by_key[scope] = record.task_id
            return record

    async def get(self, task_id: str) -> TaskRecord:
        return self._require(task_id)

    async def transition(
        self,
        *,
        task_id: str,
        expected_version: int,
        to_status: TaskStatus,
        fencing_token: int | None = None,
        terminal_reason: str | None = None,
    ) -> TransitionResult:
        async with self._lock:
            current = self._require(task_id)
            if current.status in TERMINAL_STATUSES:
                return TransitionResult(
                    applied=False,
                    winner=current,
                    rejection=TransitionRejection.TERMINAL_PROTECTED,
                )

            live = self._lease_is_live(current)
            if live and fencing_token is None:
                return TransitionResult(
                    applied=False,
                    winner=current,
                    rejection=TransitionRejection.LEASE_NOT_HELD,
                )
            if live and fencing_token != current.fencing_token:
                return TransitionResult(
                    applied=False,
                    winner=current,
                    rejection=TransitionRejection.STALE_FENCING_TOKEN,
                )
            if not live and fencing_token is not None:
                return TransitionResult(
                    applied=False,
                    winner=current,
                    rejection=TransitionRejection.LEASE_NOT_HELD,
                )

            if current.version != expected_version:
                return TransitionResult(
                    applied=False,
                    winner=current,
                    rejection=TransitionRejection.VERSION_MISMATCH,
                )
            if to_status not in ALLOWED_TRANSITIONS[current.status]:
                return TransitionResult(
                    applied=False,
                    winner=current,
                    rejection=TransitionRejection.ILLEGAL_TRANSITION,
                )

            updated = current.model_copy(
                update={
                    "status": to_status,
                    "version": current.version + 1,
                    "terminal_reason": terminal_reason,
                }
            )
            self._records[task_id] = updated
            return TransitionResult(applied=True, winner=updated, rejection=None)

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        async with self._lock:
            current = self._require(task_id)
            if current.status in TERMINAL_STATUSES:
                return None  # 已终态的任务不应再被任何 worker 领走
            if self._lease_is_live(current) and current.lease_owner != owner:
                return None
            token = self._next_token
            self._next_token += 1
            expires = self._clock() + _dt.timedelta(seconds=ttl_seconds)
            self._records[task_id] = current.model_copy(
                update={
                    "lease_owner": owner,
                    "lease_expires_at": expires,
                    "fencing_token": token,
                }
            )
            return LeaseGrant(
                task_id=task_id, owner=owner, expires_at=expires, fencing_token=token
            )

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> LeaseGrant | None:
        async with self._lock:
            current = self._require(task_id)
            if not self._lease_is_live(current):
                return None  # 过期后必须重新 acquire，否则可复活一个已被抢占的租约
            if current.lease_owner != owner or current.fencing_token != fencing_token:
                return None
            expires = self._clock() + _dt.timedelta(seconds=ttl_seconds)
            self._records[task_id] = current.model_copy(update={"lease_expires_at": expires})
            return LeaseGrant(
                task_id=task_id,
                owner=owner,
                expires_at=expires,
                fencing_token=fencing_token,
            )

    async def record_approval(self, *, request: ApprovalRequest) -> None:
        async with self._lock:
            self._approvals.append(request)

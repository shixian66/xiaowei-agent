"""``TaskStore`` 的 PostgreSQL 实现。

**判定不在这里**。拒绝顺序、fencing 闭合规则、租约与续租条件、迁移后的记录形状，
全部来自 ``persistence/decisions.py`` 的纯函数，与 ``InMemoryTaskStore`` 逐字共用。
本模块只负责三件事：把行读成 ``TaskRecord``、在正确的隔离下调用判定、把结果写回去。

**每个写方法的形状是固定的**：

    BEGIN
      SELECT ... FROM tasks WHERE task_id = :task_id FOR UPDATE
      <用 decisions.py 在 Python 侧判定>
      UPDATE ... WHERE task_id = :task_id AND version = :expected_version
      RETURNING ...
    COMMIT

``FOR UPDATE`` 让并发调用在同一行上串行化，从而使拒绝顺序与单进程实现一致；
``WHERE version = :expected_version`` 才是 CAS 本身。

**两者不可互相替代，也不可省略后者。** ``FOR UPDATE`` 之后行已被锁住，很容易觉得
"再比版本是多余的"——那正是 M3 首轮验收打回的那类空洞检查：事务内读到的 version
既是当前值又"看起来像"期望值，比较恒真，于是 CAS 永远成功、并发写全部提交，而且
所有单线程用例照常全绿。``expected_version`` 必须是**调用方传进来的那个值**。

**时钟注入而不是用 ``now()``**：租约过期是唯一与时间相关的语义，用服务端时间会让
六条既有的过期/抢占用例只能靠 ``sleep`` 才能测。注入时钟后两个实现共用同一批用例。
"""

import datetime as _dt
import uuid
from collections.abc import Mapping
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei_agent.contracts import (
    ApprovalRequest,
    LeaseGrant,
    RequestContext,
    RequestEnvelope,
    TaskRecord,
    TaskStatus,
    TransitionResult,
)
from xiaowei_agent.persistence.decisions import (
    apply_transition,
    classify_transition,
    context_matches_envelope,
    is_stale_lease,
    may_acquire_lease,
    may_renew_lease,
    stale_lease_sort_key,
)
from xiaowei_agent.persistence.rows import dump_contract, record_to_row, row_to_record
from xiaowei_agent.persistence.schema import FENCING_SEQUENCE, TASK_APPROVALS, TASKS
from xiaowei_agent.persistence.store import (
    Clock,
    ContextMismatchError,
    IdempotencyConflictError,
    LeaseCommand,
    StaleLeaseQuery,
    TaskNotFoundError,
    TransitionCommand,
    request_dedup_digest,
)

_TASK_COLUMNS: Final = tuple(column.name for column in TASKS.columns)


def _as_row(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    """``RowMapping`` → 普通 ``dict``，**按 schema 的列名逐个取**。

    不写 ``dict(mapping)``：驱动返回的映射键类型不是 ``str``，而且 ``SELECT`` 一旦
    多带回一列（比如将来加了字段），``row_to_record`` 会收到一个它不认识的键。逐列
    投影让"读哪些列"这件事在代码里可见，多出来的列被显式忽略而不是悄悄流进契约。
    """
    return {name: mapping[name] for name in _TASK_COLUMNS}


class PostgresTaskStore:
    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    # --- 读 -------------------------------------------------------------------

    async def _select_row(
        self, connection: AsyncConnection, task_id: str, *, for_update: bool
    ) -> Mapping[str, Any] | None:
        statement = sa.select(TASKS).where(TASKS.c.task_id == task_id)
        if for_update:
            statement = statement.with_for_update()
        result = await connection.execute(statement)
        row = result.mappings().first()
        return None if row is None else _as_row(row)

    async def _require_row(
        self, connection: AsyncConnection, task_id: str, *, for_update: bool
    ) -> Mapping[str, Any]:
        row = await self._select_row(connection, task_id, for_update=for_update)
        if row is None:
            raise TaskNotFoundError(task_id=task_id)
        return row

    async def get(self, task_id: str) -> TaskRecord:
        async with self._engine.connect() as connection:
            row = await self._require_row(connection, task_id, for_update=False)
        return row_to_record(row)

    async def list_stale_leases(self, *, limit: int) -> tuple[TaskRecord, ...]:
        """曾被租出、租约已过期、未终态。**不加锁、不改状态。**

        过滤条件在 SQL 里表达一次、在 ``is_stale_lease`` 里再表达一次，看似重复，
        实则各有分工：SQL 那份让数据库能用索引先砍掉绝大多数行，Python 那份是权威
        判定——两个实现共用它，因此"什么算 stale"只有一个答案。SQL 谓词只被允许
        **更宽**，收窄会让两个实现给出不同的结果集。
        """
        query = StaleLeaseQuery(limit=limit)
        now = self._clock()
        statement = (
            sa.select(TASKS)
            .where(
                TASKS.c.fencing_token.is_not(None),
                TASKS.c.lease_expires_at <= now,
            )
            .order_by(TASKS.c.lease_expires_at, TASKS.c.task_id)
            .limit(query.limit)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        records = [row_to_record(_as_row(row)) for row in rows]
        stale = [record for record in records if is_stale_lease(record, now=now)]
        return tuple(sorted(stale, key=stale_lease_sort_key))

    # --- 写 -------------------------------------------------------------------

    async def create_task(
        self, *, envelope: RequestEnvelope, context: RequestContext
    ) -> TaskRecord:
        """幂等创建。**并发重复请求只产生一个任务事实。**

        用 ``ON CONFLICT DO NOTHING`` 而不是"先查后插"：后者两步之间有窗口，两个
        连接会双双查不到、双双插入，其中一个撞唯一约束并把数据库错误抛给调用方
        ——而调用方看到的应该是"你的任务已经在这儿了"，不是一个 IntegrityError。

        冲突时不返回行，此时再按幂等作用域读回既存记录并比对摘要。
        """
        if not context_matches_envelope(envelope, context):
            raise ContextMismatchError("envelope and context disagree on execution context")
        digest = request_dedup_digest(envelope, context)
        candidate = TaskRecord(
            task_id=str(uuid.uuid4()),
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            actor=context.actor,
            idempotency_key=envelope.idempotency_key,
            request_digest=digest,
            status=TaskStatus.CREATED,
            version=0,
        )
        insert = (
            sa.dialects.postgresql.insert(TASKS)
            .values(record_to_row(candidate))
            .on_conflict_do_nothing(constraint="uq_tasks_idempotency_scope")
            .returning(TASKS)
        )
        async with self._engine.begin() as connection:
            inserted = (await connection.execute(insert)).mappings().first()
            if inserted is not None:
                return row_to_record(_as_row(inserted))
            existing = (
                (
                    await connection.execute(
                        sa.select(TASKS).where(
                            TASKS.c.tenant_id == context.tenant_id,
                            TASKS.c.environment_id == context.environment_id,
                            TASKS.c.idempotency_key == envelope.idempotency_key,
                        )
                    )
                )
                .mappings()
                .one()
            )
        record = row_to_record(_as_row(existing))
        if record.request_digest != digest:
            raise IdempotencyConflictError("idempotency key reused for a different request")
        return record

    async def transition(
        self,
        *,
        task_id: str,
        expected_version: int,
        to_status: TaskStatus,
        fencing_token: int | None = None,
        terminal_reason: str | None = None,
    ) -> TransitionResult:
        # 命令 DTO 必须在**开事务之前**构造：反过来会在参数非法时先开事务、可能已经
        # 写入，再抛 ValidationError，留下一条带无效字段的记录。
        command = TransitionCommand(
            task_id=task_id,
            expected_version=expected_version,
            to_status=to_status,
            fencing_token=fencing_token,
            terminal_reason=terminal_reason,
        )
        async with self._engine.begin() as connection:
            current = row_to_record(
                await self._require_row(connection, command.task_id, for_update=True)
            )
            rejection = classify_transition(current, command, now=self._clock())
            if rejection is not None:
                return TransitionResult(applied=False, winner=current, rejection=rejection)
            updated = apply_transition(current, command)
            row = (
                (
                    await connection.execute(
                        sa.update(TASKS)
                        .where(
                            TASKS.c.task_id == command.task_id,
                            # 调用方传入的值，不是刚读到的值。见模块 docstring。
                            TASKS.c.version == command.expected_version,
                        )
                        .values(
                            status=updated.status.value,
                            version=updated.version,
                            # 无条件写入，包括写 NULL：见 decisions.apply_transition。
                            terminal_reason=updated.terminal_reason,
                        )
                        .returning(TASKS)
                    )
                )
                .mappings()
                .one()
            )
        return TransitionResult(
            applied=True, winner=row_to_record(_as_row(row)), rejection=None
        )

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        command = LeaseCommand(task_id=task_id, owner=owner, ttl_seconds=ttl_seconds)
        async with self._engine.begin() as connection:
            current = row_to_record(
                await self._require_row(connection, command.task_id, for_update=True)
            )
            now = self._clock()
            if not may_acquire_lease(current, owner=command.owner, now=now):
                return None
            token_result = await connection.execute(
                sa.select(FENCING_SEQUENCE.next_value())
            )
            token = token_result.scalar_one()
            expires = now + _dt.timedelta(seconds=command.ttl_seconds)
            # 先构造并校验 grant，再写库：反过来会在取值非法时先污染存储。
            grant = LeaseGrant(
                task_id=command.task_id,
                owner=command.owner,
                expires_at=expires,
                fencing_token=token,
            )
            await connection.execute(
                sa.update(TASKS)
                .where(TASKS.c.task_id == command.task_id)
                .values(lease_owner=command.owner, lease_expires_at=expires, fencing_token=token)
            )
        return grant

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> LeaseGrant | None:
        command = LeaseCommand(
            task_id=task_id, owner=owner, ttl_seconds=ttl_seconds, fencing_token=fencing_token
        )
        token = command.fencing_token if command.fencing_token is not None else 0
        async with self._engine.begin() as connection:
            current = row_to_record(
                await self._require_row(connection, command.task_id, for_update=True)
            )
            now = self._clock()
            if not may_renew_lease(current, owner=command.owner, fencing_token=token, now=now):
                return None
            expires = now + _dt.timedelta(seconds=command.ttl_seconds)
            grant = LeaseGrant(
                task_id=command.task_id,
                owner=command.owner,
                expires_at=expires,
                fencing_token=token,
            )
            # 只续期，不动 owner 与 token：token 在**取得**租约时才递增。
            await connection.execute(
                sa.update(TASKS)
                .where(TASKS.c.task_id == command.task_id)
                .values(lease_expires_at=expires)
            )
        return grant

    async def record_approval(self, *, request: ApprovalRequest) -> None:
        """append-only 写入。

        ``seq`` 在同一事务里由 ``MAX(seq) + 1`` 取得，并先对任务行加 ``FOR UPDATE``
        使同一任务的并发写入串行化。**已知边界**：任务行不存在时没有可锁的行，此时
        并发写入同一 ``task_id`` 会撞主键——与内存实现一样，本方法不校验任务存在性，
        而现实里审批总是属于一个已创建的任务。
        """
        async with self._engine.begin() as connection:
            await self._select_row(connection, request.task_id, for_update=True)
            next_seq = sa.select(
                sa.func.coalesce(sa.func.max(TASK_APPROVALS.c.seq), 0) + 1
            ).where(TASK_APPROVALS.c.task_id == request.task_id)
            await connection.execute(
                sa.insert(TASK_APPROVALS).values(
                    task_id=request.task_id,
                    step_id=request.step_id,
                    seq=next_seq.scalar_subquery(),
                    state=request.state.value,
                    request=dump_contract(request),
                )
            )

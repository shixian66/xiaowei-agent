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
    TERMINAL_STATUSES,
    ApprovalRequest,
    EvidenceEnvelope,
    ExecutionPlan,
    LeaseGrant,
    ResolvedTarget,
    TaskLookup,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TraceEvent,
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
from xiaowei_agent.persistence.evidence import (
    EvidenceConflictError,
    EvidenceNotFoundError,
)
from xiaowei_agent.persistence.plans import (
    PlanConflictError,
    PlanNotFoundError,
    StoredPlan,
)
from xiaowei_agent.persistence.rows import (
    dump_contract,
    load_contract,
    record_to_row,
    row_to_record,
)
from xiaowei_agent.persistence.schema import (
    CREATED_SEQUENCE,
    FENCING_SEQUENCE,
    TASK_APPROVALS,
    TASK_AUDIT_EVENTS,
    TASK_EVIDENCE,
    TASK_PLANS,
    TASK_SUBMISSIONS,
    TASKS,
)
from xiaowei_agent.persistence.store import (
    Clock,
    ContextMismatchError,
    IdempotencyConflictError,
    LeaseCommand,
    StaleLeaseQuery,
    TaskNotFoundError,
    TransitionCommand,
    UnscopedAuditEventError,
    idempotency_scope_digest,
    request_dedup_digest,
    submission_digest,
)

_TASK_COLUMNS: Final = tuple(column.name for column in TASKS.columns)


async def _serialise_on_task(connection: AsyncConnection, task_id: str) -> None:
    """按 ``task_id`` 取一把**事务级** advisory lock。

    两张 append-only 表用 ``MAX(seq) + 1`` 分配序号，这是读-改-写：并发写入同一任务
    会算出同一个 seq，然后其中一个撞唯一约束，把数据库错误抛给调用方。

    不用 ``SELECT ... FOR UPDATE`` 锁 ``tasks`` 行：那要求任务行已存在，而这两个
    台账在契约上都不校验任务存在性（内存实现也不校验）。advisory lock 不依赖任何
    行，事务结束自动释放。哈希碰撞只会让两个无关任务互相串行化，无害。
    """
    await connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtext(:task_id))"), {"task_id": task_id}
    )


def _next_seq(table: sa.Table, task_id: str) -> sa.ScalarSelect[int]:
    """``MAX(seq) + 1``，作用域限定在单个 ``task_id`` 内。

    两张 append-only 表共用这一份，理由与内存实现里的 ``_append`` 相同：编号规则
    分成两份就会分叉，而"审批和审计的序号规则不一样"不会让任何用例失败。

    读-改-写不是原子的，必须在 :func:`_serialise_on_task` 的 advisory lock 之下调用；
    单独用它会让并发写入算出同一个 seq 并撞主键。
    """
    return (
        sa.select(sa.func.coalesce(sa.func.max(table.c.seq), 0) + 1)
        .where(table.c.task_id == task_id)
        .scalar_subquery()
    )


def stale_lease_statement(*, now: _dt.datetime, limit: int) -> sa.Select[Any]:
    """``list_stale_leases`` 的查询。**谓词必须与 ``is_stale_lease`` 逐条等价。**

    此前这里的谓词只有"曾被租出 + 已过期"，把"未终态"留给 Python 侧过滤，依据是
    「SQL 谓词只被允许更宽」。**那条规则在有 ``LIMIT`` 的前提下不成立**：更宽的谓词
    会让 ``LIMIT`` 被将要被丢弃的行吃掉，于是真正需要恢复的任务被挤出窗口。

    这不是罕见边界。``apply_transition`` **有意不清租约字段**（清空会重开 fencing 的
    缺口，见 ``is_stale_lease``），因此**每一个正常结束的任务都永久命中"曾被租出 +
    已过期"**，并且按 ``lease_expires_at`` 升序排在最前。它们只增不减，于是
    ``list_stale_leases`` 的返回量会随系统运行**单调衰减到零**——这是长期运行的稳态，
    不是边界情况。内存实现是"先过滤、再排序、再截断"，因此两个实现已经分叉。

    修法是让谓词等价，而不是"多取一些再截断"：多取多少才够是没有答案的，终态任务
    的数量没有上界。

    **终态集合从 ``TERMINAL_STATUSES`` 派生**，不在这里重列：新增一个终态时，这条
    查询自动跟上。理由与 ``test_row_mapping.py`` 从 ``ALL_TABLES`` 扫 JSONB 列相同。

    三个租约字段的判定写成 ``NOT (owner IS NOT NULL AND expires IS NOT NULL AND
    expires > now)``，而不是简写成 ``expires <= now``：后者在 ``expires IS NULL`` 时
    求值为 NULL 而非 TRUE，会**收窄**谓词——那正是被禁的方向。写成前者之后，SQL 与
    ``lease_is_live`` 的取反逐字对应，且不依赖"三字段同置同清"这条不变量成立。
    """
    lease_is_live_sql = sa.and_(
        TASKS.c.lease_owner.is_not(None),
        TASKS.c.lease_expires_at.is_not(None),
        TASKS.c.lease_expires_at > now,
    )
    return (
        sa.select(TASKS)
        .where(
            TASKS.c.fencing_token.is_not(None),
            sa.not_(lease_is_live_sql),
            TASKS.c.status.not_in(sorted(status.value for status in TERMINAL_STATUSES)),
        )
        .order_by(TASKS.c.lease_expires_at, TASKS.c.task_id)
        .limit(limit)
    )


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

    async def get(self, *, lookup: TaskLookup) -> TaskRecord:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(TASKS).where(
                    TASKS.c.task_id == lookup.task_id,
                    TASKS.c.tenant_id == lookup.tenant_id,
                    TASKS.c.environment_id == lookup.environment_id,
                )
            )
            found = result.mappings().first()
            if found is None:
                raise TaskNotFoundError(task_id=lookup.task_id)
            row = _as_row(found)
        return row_to_record(row)

    async def list_stale_leases(self, *, limit: int) -> tuple[TaskRecord, ...]:
        """曾被租出、租约已过期、未终态。**不加锁、不改状态。**

        SQL 谓词与 ``is_stale_lease`` 逐条等价，理由见 :func:`stale_lease_statement`。
        Python 侧仍再过滤一次：那是两个实现共用的权威判定，"什么算 stale"只能有一个
        答案。等价之后这一步在正常数据上是空操作，但它是 SQL 一旦漂移时的兜底。
        """
        query = StaleLeaseQuery(limit=limit)
        now = self._clock()
        statement = stale_lease_statement(now=now, limit=query.limit)
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        records = [row_to_record(_as_row(row)) for row in rows]
        stale = [record for record in records if is_stale_lease(record, now=now)]
        return tuple(sorted(stale, key=stale_lease_sort_key))

    # --- 写 -------------------------------------------------------------------

    async def create_task(self, *, submission: TaskSubmission) -> TaskRecord:
        """幂等创建。**并发重复请求只产生一个任务事实。**

        用 ``ON CONFLICT DO NOTHING`` 而不是"先查后插"：后者两步之间有窗口，两个
        连接会双双查不到、双双插入，其中一个撞唯一约束并把数据库错误抛给调用方
        ——而调用方看到的应该是"你的任务已经在这儿了"，不是一个 IntegrityError。

        冲突时不返回行，此时再按幂等作用域读回既存记录并比对摘要。
        """
        envelope = submission.envelope
        request_context = submission.context
        if not context_matches_envelope(envelope, request_context):
            raise ContextMismatchError("envelope and context disagree on execution context")
        digest = request_dedup_digest(envelope, request_context)
        scope_digest = idempotency_scope_digest(
            tenant_id=request_context.tenant_id,
            environment_id=request_context.environment_id,
            idempotency_key=envelope.idempotency_key,
        )
        async with self._engine.begin() as connection:
            created_seq = (
                await connection.execute(sa.select(CREATED_SEQUENCE.next_value()))
            ).scalar_one()
            candidate = TaskRecord(
                task_id=str(uuid.uuid4()),
                tenant_id=request_context.tenant_id,
                environment_id=request_context.environment_id,
                actor=request_context.actor,
                idempotency_key=envelope.idempotency_key,
                request_digest=digest,
                status=TaskStatus.CREATED,
                version=0,
                created_seq=created_seq,
                attempt_number=0,
                task_failure_count=0,
                next_attempt_at=None,
            )
            insert = (
                sa.dialects.postgresql.insert(TASKS)
                .values(
                    **record_to_row(candidate),
                    idempotency_scope_digest=scope_digest,
                )
                .on_conflict_do_nothing(constraint="uq_tasks_idempotency_scope_digest")
                .returning(TASKS)
            )
            inserted = (await connection.execute(insert)).mappings().first()
            if inserted is not None:
                await connection.execute(
                    sa.insert(TASK_SUBMISSIONS).values(
                        task_id=candidate.task_id,
                        envelope=dump_contract(submission.envelope),
                        context=dump_contract(submission.context),
                        as_of=submission.as_of,
                        submission_digest=submission_digest(submission),
                    )
                )
                return row_to_record(_as_row(inserted))
            existing = (
                (
                    await connection.execute(
                        sa.select(TASKS).where(
                            TASKS.c.idempotency_scope_digest == scope_digest
                        )
                    )
                )
                .mappings()
                .one()
            )
        record = row_to_record(_as_row(existing))
        same_scope = (
            record.tenant_id == request_context.tenant_id
            and record.environment_id == request_context.environment_id
            and record.idempotency_key == envelope.idempotency_key
        )
        if not same_scope or record.request_digest != digest:
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

    async def record_approval(self, *, request: ApprovalRequest) -> int:
        """append-only 写入。``seq`` 由 ``MAX(seq) + 1`` 分配，并发由 advisory lock 串行化。

        ``RETURNING seq`` 取回数据库真正分配到的序号，而不是在 Python 侧再算一遍：
        再算一遍就是空洞检查——两侧同源，比对恒真。
        """
        async with self._engine.begin() as connection:
            await _serialise_on_task(connection, request.task_id)
            seq = (
                await connection.execute(
                    sa.insert(TASK_APPROVALS)
                    .values(
                        task_id=request.task_id,
                        step_id=request.step_id,
                        seq=_next_seq(TASK_APPROVALS, request.task_id),
                        state=request.state.value,
                        request=dump_contract(request),
                    )
                    .returning(TASK_APPROVALS.c.seq)
                )
            ).scalar_one()
        return int(seq)

    async def record_audit_event(self, *, event: TraceEvent) -> int:
        """append-only 写入审计事件。形状与 :meth:`record_approval` 逐字一致。

        ``stage`` / ``outcome`` / ``occurred_at`` 从 JSONB 载荷里**提升成列**，是为了让
        "某任务在某阶段发生过什么"不必每次都解 JSON，并为将来建索引留出可索引的投影。
        **目前这三列上没有索引**（schema 与迁移里只有主键 ``(task_id, seq)``）：按 ``task_id``
        过滤走主键，按 ``stage`` / ``outcome`` 过滤仍是顺序扫描。消费路径归 M8，届时按真实
        查询形状再决定索引，现在建等于凭空猜。载荷本身整条存下，提升列只是它的投影，
        不是另一份真相——一致性由 integration 的
        ``test_promoted_columns_agree_with_the_stored_payload`` 承重。
        """
        if event.task_id is None:
            raise UnscopedAuditEventError("audit event has no task_id")
        task_id = event.task_id
        async with self._engine.begin() as connection:
            await _serialise_on_task(connection, task_id)
            seq = (
                await connection.execute(
                    sa.insert(TASK_AUDIT_EVENTS)
                    .values(
                        task_id=task_id,
                        seq=_next_seq(TASK_AUDIT_EVENTS, task_id),
                        stage=event.stage.value,
                        outcome=event.outcome.value,
                        occurred_at=event.occurred_at,
                        event=dump_contract(event),
                    )
                    .returning(TASK_AUDIT_EVENTS.c.seq)
                )
            ).scalar_one()
        return int(seq)


class PostgresPlanStore:
    """``PlanStore`` 的 PostgreSQL 实现。

    ``save`` 用 ``ON CONFLICT DO NOTHING`` 后比对，而不是"先查后插"：并发的两次
    ``save`` 会双双查不到、双双插入，其中一个撞主键。冲突后读回既存内容再比较，
    内容相同即幂等成功，不同才是 ``PlanConflictError``——重试不该被当成篡改。
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save(
        self, *, task_id: str, plan: ExecutionPlan, target: ResolvedTarget
    ) -> None:
        candidate = StoredPlan(plan=plan, target=target)
        insert = (
            sa.dialects.postgresql.insert(TASK_PLANS)
            .values(
                task_id=task_id,
                plan=dump_contract(plan),
                target=dump_contract(target),
            )
            .on_conflict_do_nothing(index_elements=["task_id"])
            .returning(TASK_PLANS.c.task_id)
        )
        async with self._engine.begin() as connection:
            inserted = (await connection.execute(insert)).first()
            if inserted is not None:
                return
            existing = await self._load_row(connection, task_id)
        if existing is None or existing != candidate:
            raise PlanConflictError(
                "a different plan is already stored for this task", task_id=task_id
            )

    async def _load_row(
        self, connection: AsyncConnection, task_id: str
    ) -> StoredPlan | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_PLANS.c.plan, TASK_PLANS.c.target).where(
                        TASK_PLANS.c.task_id == task_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return StoredPlan(
            plan=load_contract(ExecutionPlan, row["plan"]),
            target=load_contract(ResolvedTarget, row["target"]),
        )

    async def load(self, *, task_id: str) -> StoredPlan:
        async with self._engine.connect() as connection:
            stored = await self._load_row(connection, task_id)
        if stored is None:
            raise PlanNotFoundError("no plan stored for this task", task_id=task_id)
        return stored


class PostgresEvidenceLedger:
    """``EvidenceLedger`` 的 PostgreSQL 实现。

    ``load`` 按 ``seq`` 排序，不按 ``captured_at``：契约是"按写入顺序返回"，而同一
    毫秒采集的两条证据时间戳可能相同，用时间排序会让顺序在两次读取之间变化。
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    async def append(self, *, task_id: str, envelope: EvidenceEnvelope) -> str:
        payload = dump_contract(envelope)
        next_seq = sa.select(
            sa.func.coalesce(sa.func.max(TASK_EVIDENCE.c.seq), 0) + 1
        ).where(TASK_EVIDENCE.c.task_id == task_id)
        insert = (
            sa.dialects.postgresql.insert(TASK_EVIDENCE)
            .values(
                task_id=task_id,
                evidence_id=envelope.evidence_id,
                seq=next_seq.scalar_subquery(),
                envelope=payload,
            )
            .on_conflict_do_nothing(index_elements=["task_id", "evidence_id"])
            .returning(TASK_EVIDENCE.c.evidence_id)
        )
        async with self._engine.begin() as connection:
            await _serialise_on_task(connection, task_id)
            inserted = (await connection.execute(insert)).first()
            if inserted is not None:
                return envelope.evidence_id
            existing = (
                (
                    await connection.execute(
                        sa.select(TASK_EVIDENCE.c.envelope).where(
                            TASK_EVIDENCE.c.task_id == task_id,
                            TASK_EVIDENCE.c.evidence_id == envelope.evidence_id,
                        )
                    )
                )
                .mappings()
                .one()
            )
        # 内容相同的重写是幂等的（重试不该被当成篡改）；不同则一律拒绝。
        if load_contract(EvidenceEnvelope, existing["envelope"]) != envelope:
            raise EvidenceConflictError(
                "different evidence already recorded under this id", task_id=task_id
            )
        return envelope.evidence_id

    async def load(self, *, task_id: str) -> tuple[EvidenceEnvelope, ...]:
        """无证据时返回空元组，**不抛异常**——没取过数是正常状态，不是错误。"""
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(TASK_EVIDENCE.c.envelope)
                        .where(TASK_EVIDENCE.c.task_id == task_id)
                        .order_by(TASK_EVIDENCE.c.seq)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(load_contract(EvidenceEnvelope, row["envelope"]) for row in rows)

    async def get(self, *, task_id: str, evidence_id: str) -> EvidenceEnvelope:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(TASK_EVIDENCE.c.envelope).where(
                            TASK_EVIDENCE.c.task_id == task_id,
                            TASK_EVIDENCE.c.evidence_id == evidence_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise EvidenceNotFoundError(
                "no evidence recorded under this reference", task_id=task_id
            )
        return load_contract(EvidenceEnvelope, row["envelope"])

"""``TaskStore`` 与 ``ChannelStore`` 的 PostgreSQL 实现。

**判定不在这里**。拒绝顺序、fencing 闭合规则、租约与续租条件、迁移后的记录形状，
全部来自 ``persistence/decisions.py`` 的纯函数，与 ``InMemoryTaskStore`` 逐字共用。
本模块只负责三件事：把行读成契约、在正确的隔离下调用判定、把结果写回去。

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
import functools
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final, ParamSpec, TypeVar

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ActorTaskPageQuery,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    ApprovalRequest,
    AttemptIntent,
    ChannelKind,
    ClarificationRecord,
    EvidenceEnvelope,
    ExecutionPlan,
    GrantRejection,
    IdentitySource,
    LeaseGrant,
    ProductRole,
    ProjectionState,
    RequestContext,
    RequestEnvelope,
    ResolvedTarget,
    RetryDecision,
    ScopeTaskPageQuery,
    StepAttemptDecision,
    StepCommitRejection,
    StoredTaskPage,
    StoredTaskRead,
    TaskAttemptRejection,
    TaskLookup,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TraceEvent,
    TransitionResult,
    UserStatus,
)
from xiaowei_agent.contracts.activation import (
    ActivationLookup,
    ActivationRequest,
    ActivationStatus,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEvent,
    AdminAuditStart,
    AdminAuditTerminal,
    AdminOperationContext,
)
from xiaowei_agent.contracts.identity import (
    ApproveActivationCommand,
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    DirectoryCommand,
    DirectoryPrincipalFacts,
    MigrateLegacyIdentitiesCommand,
    RejectActivationCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UserAccount,
    UserRoleAssignment,
)
from xiaowei_agent.persistence.activation import (
    ACTIVATION_TTL_SECONDS,
    MAX_PENDING_ACTIVATIONS,
    ActivationCapacityError,
    activation_source_ref_digest,
    activation_subject_digest,
)
from xiaowei_agent.persistence.admin_audit import (
    AdminAuditConflictError,
    AdminAuditMissingStartError,
    seal,
)
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    ChannelBinding,
    ChannelBindingConflictError,
    ChannelBindingLookup,
    ChannelBindingNotFoundError,
    ClaimedTaskLookup,
    ClaimProjectionCommand,
    CompleteProjectionCommand,
    CreateProjectionSubscriptionCommand,
    DeadLetterProjectionCommand,
    GroupBindingLookup,
    GroupBoundTaskIdsQuery,
    ProjectionClaimMutation,
    ProjectionClaimNotFoundError,
    ProjectionDueQuery,
    ProjectionSubscription,
    ProjectionSubscriptionConflictError,
    ProjectionSubscriptionNotFoundError,
    ProjectionUpdateResult,
    RecordInitialProjectionCommand,
    RenewProjectionClaimCommand,
    ScheduleProviderRetryCommand,
    ScheduleTaskRecheckCommand,
    authorize_claimed_task_lookup,
    binding_matches_command,
    claim_projection,
    complete_projection,
    dead_letter_projection,
    record_initial_projection,
    renew_projection_claim,
    schedule_provider_retry,
    schedule_task_recheck,
    subscription_matches_command,
)
from xiaowei_agent.persistence.clarification_records import (
    ClarificationRecordCandidate,
    ClarificationRecordConflictError,
    record_matches_candidate,
    require_clarification_record_grant,
)
from xiaowei_agent.persistence.decisions import (
    apply_transition,
    attempt_statuses,
    classify_task_attempt,
    classify_transition,
    context_matches_envelope,
    dispatch_sort_key,
    grant_is_current,
    is_dispatchable,
    is_stale_lease,
    may_acquire_lease,
    may_renew_lease,
    stale_lease_sort_key,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
    classify_persistence_exception,
)
from xiaowei_agent.persistence.evidence import (
    EvidenceConflictError,
    EvidenceNotFoundError,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryDecisionDeniedError,
    UserDirectoryNotFoundError,
    UserDirectorySubjectUnavailableError,
    activation_user_id,
    batch_operation_id,
    derive_audit,
    external_subject_digest,
)
from xiaowei_agent.persistence.local_admin import LOCAL_ADMIN_SINGLETON_ID
from xiaowei_agent.persistence.model_artifacts import (
    AcceptedInteractionArtifact,
    AdvisoryArtifactCandidate,
    InteractionArtifactCandidate,
    ModelArtifactConflictError,
    StoredModelAdvisory,
    artifact_matches_candidate,
    require_model_artifact_grant,
)
from xiaowei_agent.persistence.plans import (
    PlanConflictError,
    PlanNotFoundError,
    StoredPlan,
    reject_unsupported_plan_schema,
)
from xiaowei_agent.persistence.rows import (
    activation_request_to_row,
    admin_audit_event_to_row,
    channel_binding_to_row,
    clarification_record_to_row,
    dump_contract,
    interaction_artifact_to_row,
    load_contract,
    model_advisory_to_row,
    oauth_state_to_row,
    projection_subscription_to_row,
    record_to_row,
    row_to_activation_request,
    row_to_admin_audit_event,
    row_to_channel_binding,
    row_to_clarification_record,
    row_to_interaction_artifact,
    row_to_model_advisory,
    row_to_oauth_state,
    row_to_projection_subscription,
    row_to_record,
    row_to_step_execution,
    row_to_web_session,
    step_execution_to_row,
    web_session_to_row,
)
from xiaowei_agent.persistence.schema import (
    ACTIVATION_REQUESTS,
    ADMIN_AUDIT_EVENTS,
    CHANNEL_BINDINGS,
    CREATED_SEQUENCE,
    EXTERNAL_IDENTITIES,
    FENCING_SEQUENCE,
    LOCAL_ADMINS,
    PROJECTION_FENCING_SEQUENCE,
    PROJECTION_SUBSCRIPTIONS,
    TASK_APPROVALS,
    TASK_AUDIT_EVENTS,
    TASK_CLARIFICATION_RECORDS,
    TASK_EVIDENCE,
    TASK_INTERACTION_ARTIFACTS,
    TASK_MODEL_ADVISORIES,
    TASK_PLANS,
    TASK_STEP_EXECUTIONS,
    TASK_SUBMISSIONS,
    TASKS,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
    WEB_OAUTH_STATES,
    WEB_SESSIONS,
)
from xiaowei_agent.persistence.store import (
    ClarificationParentRequiredError,
    Clock,
    ContextMismatchError,
    DispatchQuery,
    IdempotencyConflictError,
    LeaseCommand,
    RetryCommand,
    RetryResult,
    StaleLeaseQuery,
    StepAttemptCommand,
    StepAttemptResult,
    StepCommitCommand,
    StepCommitResult,
    StepExecutionRecord,
    TaskAttemptCommand,
    TaskAttemptGrant,
    TaskAttemptResult,
    TaskNotFoundError,
    TransitionCommand,
    UnscopedAuditEventError,
    attempt_terminal_event,
    clarification_parent_is_usable,
    idempotency_scope_digest,
    reject_clarification_parent_on_plain_create,
    request_dedup_digest,
    retry_command_digest,
    step_commit_digest,
    submission_digest,
    submission_matches_record,
    validate_task_failure_limit,
)
from xiaowei_agent.persistence.web_session import (
    DEFAULT_OAUTH_STATE_CAPACITY,
    ConsumeOAuthStateCommand,
    IssueOAuthStateCommand,
    OAuthState,
    OAuthStateCapacityError,
    OAuthStateNotFoundError,
    RevokeWebSessionCommand,
    RotateWebSessionCommand,
    WebSession,
    WebSessionConflictError,
    WebSessionLookup,
    WebSessionNotFoundError,
    validate_oauth_state_capacity,
)

_TASK_COLUMNS: Final = tuple(column.name for column in TASKS.columns)
_OAUTH_STATE_CAPACITY_LOCK: Final = sa.text(
    "SELECT pg_advisory_xact_lock(2026091001)"
)
ACTIVATION_CAPACITY_LOCK_KEY: Final[int] = 2026092201
"""激活待办全局容量的固定事务锁键，供真库并发测试抢同一把锁。"""

_ACTIVATION_CAPACITY_LOCK: Final = sa.text(
    "SELECT pg_advisory_xact_lock(:lock_key)"
).bindparams(lock_key=ACTIVATION_CAPACITY_LOCK_KEY)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _persistence_boundary(
    *, write: bool
) -> Callable[
    [Callable[_P, Coroutine[Any, Any, _R]]],
    Callable[_P, Coroutine[Any, Any, _R]],
]:
    """把驱动异常收敛为闭集错误，并切断可能携带 SQL/参数的异常链。"""

    def decorate(
        operation: Callable[_P, Coroutine[Any, Any, _R]],
    ) -> Callable[_P, Coroutine[Any, Any, _R]]:
        @functools.wraps(operation)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            mapped: PersistenceUnavailableError | PersistenceIntegrityError | None = None
            try:
                return await operation(*args, **kwargs)
            except Exception as exc:
                mapped = (
                    exc
                    if isinstance(
                        exc, (PersistenceUnavailableError, PersistenceIntegrityError)
                    )
                    else classify_persistence_exception(
                        exc,
                        write_outcome=(
                            PersistenceWriteOutcome.NOT_CONFIRMED if write else None
                        ),
                    )
                )
                if mapped is None:
                    raise
            if isinstance(mapped, PersistenceUnavailableError):
                raise PersistenceUnavailableError(
                    category=mapped.category,
                    write_outcome=mapped.write_outcome,
                )
            if isinstance(mapped, PersistenceIntegrityError):
                raise PersistenceIntegrityError(
                    category=mapped.category,
                    write_outcome=mapped.write_outcome,
                )
            raise RuntimeError("persistence exception classification failed")

        return wrapped

    return decorate


@asynccontextmanager
async def _write_transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """打开写事务，并以事务退出事实区分回滚与未知提交结果。"""
    entered = False
    body_error: Exception | None = None
    mapped: PersistenceUnavailableError | PersistenceIntegrityError | None = None
    try:
        async with engine.begin() as connection:
            entered = True
            try:
                yield connection
            except Exception as exc:
                body_error = exc
                raise
    except Exception as exc:
        # 未进入事务就失败时没有语句被执行；正文异常原样穿过 __aexit__ 则证明
        # rollback 已完成。commit/rollback 阶段换成了另一异常时，结果不可确认。
        rolled_back = not entered or exc is body_error
        mapped = classify_persistence_exception(
            exc,
            write_outcome=(
                PersistenceWriteOutcome.ROLLED_BACK
                if rolled_back
                else PersistenceWriteOutcome.NOT_CONFIRMED
            ),
        )
        if mapped is None:
            raise
    if mapped is None:
        return
    if isinstance(mapped, PersistenceUnavailableError):
        raise PersistenceUnavailableError(
            category=mapped.category,
            write_outcome=mapped.write_outcome,
        )
    if isinstance(mapped, PersistenceIntegrityError):
        raise PersistenceIntegrityError(
            category=mapped.category,
            write_outcome=mapped.write_outcome,
        )


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


async def _append_audit_events(
    connection: AsyncConnection, *, task_id: str, events: tuple[TraceEvent, ...]
) -> tuple[int, ...]:
    """在调用方事务内追加一组审计；整组与 aggregate 写入同生共死。"""
    if any(event.task_id != task_id for event in events):
        raise ValueError("audit event belongs to a different task")
    if not events:
        return ()
    await _serialise_on_task(connection, task_id)
    seqs: list[int] = []
    for event in events:
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
        seqs.append(int(seq))
    return tuple(seqs)


async def _append_evidence(
    connection: AsyncConnection,
    *,
    task_id: str,
    envelope: EvidenceEnvelope,
) -> str:
    """在调用方事务内幂等追加 Evidence；与公开 ledger 共用同一条写路径。"""
    await _serialise_on_task(connection, task_id)
    next_seq = sa.select(
        sa.func.coalesce(sa.func.max(TASK_EVIDENCE.c.seq), 0) + 1
    ).where(TASK_EVIDENCE.c.task_id == task_id)
    insert = (
        sa.dialects.postgresql.insert(TASK_EVIDENCE)
        .values(
            task_id=task_id,
            evidence_id=envelope.evidence_id,
            seq=next_seq.scalar_subquery(),
            envelope=dump_contract(envelope),
        )
        .on_conflict_do_nothing(index_elements=["task_id", "evidence_id"])
        .returning(TASK_EVIDENCE.c.evidence_id)
    )
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
    if load_contract(EvidenceEnvelope, existing["envelope"]) != envelope:
        raise EvidenceConflictError(
            "different evidence already recorded under this id", task_id=task_id
        )
    return envelope.evidence_id


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


class PostgresChannelStore:
    """ChannelStore 的 PostgreSQL 实现；所有竞争更新均锁定订阅行。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def _require_task_scope(
        self,
        connection: AsyncConnection,
        *,
        task_id: str,
        tenant_id: str | None = None,
        environment_id: str | None = None,
    ) -> None:
        conditions = [TASKS.c.task_id == task_id]
        if tenant_id is not None:
            conditions.append(TASKS.c.tenant_id == tenant_id)
        if environment_id is not None:
            conditions.append(TASKS.c.environment_id == environment_id)
        found = await connection.scalar(sa.select(TASKS.c.task_id).where(*conditions))
        if found is None:
            raise TaskNotFoundError(task_id=task_id)

    async def _create_subscription(
        self,
        connection: AsyncConnection,
        command: CreateProjectionSubscriptionCommand,
    ) -> ProjectionSubscription:
        await self._require_task_scope(connection, task_id=command.task_id)
        candidate = ProjectionSubscription(
            subscription_id=str(uuid.uuid4()),
            task_id=command.task_id,
            destination_kind=command.destination_kind,
            destination_ref=command.destination_ref,
            state=command.initial_state,
            attempt_number=0,
            next_attempt_at=command.next_attempt_at,
            provider_failure_count=0,
        )
        insert = (
            sa.dialects.postgresql.insert(PROJECTION_SUBSCRIPTIONS)
            .values(**projection_subscription_to_row(candidate))
            .on_conflict_do_nothing()
            .returning(PROJECTION_SUBSCRIPTIONS)
        )
        inserted = (await connection.execute(insert)).mappings().first()
        if inserted is not None:
            return row_to_projection_subscription(inserted)
        existing = (
            (
                await connection.execute(
                    sa.select(PROJECTION_SUBSCRIPTIONS)
                    .where(
                        PROJECTION_SUBSCRIPTIONS.c.task_id == command.task_id,
                        PROJECTION_SUBSCRIPTIONS.c.destination_kind
                        == command.destination_kind.value,
                        PROJECTION_SUBSCRIPTIONS.c.destination_ref
                        == command.destination_ref,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise ProjectionSubscriptionConflictError
        winner = row_to_projection_subscription(existing)
        if not subscription_matches_command(winner, command):
            raise ProjectionSubscriptionConflictError
        return winner

    @_persistence_boundary(write=True)
    async def bind_task(self, *, command: BindTaskCommand) -> ChannelBinding:
        async with _write_transaction(self._engine) as connection:
            await self._require_task_scope(
                connection,
                task_id=command.task_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
            )
            candidate = ChannelBinding(
                binding_id=str(uuid.uuid4()),
                task_id=command.task_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                channel=command.channel,
                initiator_subject_ref=command.initiator_subject_ref,
                conversation_ref=command.conversation_ref,
                source_event_ref=command.source_event_ref,
                created_at=command.created_at,
            )
            insert = (
                sa.dialects.postgresql.insert(CHANNEL_BINDINGS)
                .values(**channel_binding_to_row(candidate))
                .on_conflict_do_nothing()
                .returning(CHANNEL_BINDINGS)
            )
            inserted = (await connection.execute(insert)).mappings().first()
            if inserted is None:
                rows = (
                    (
                        await connection.execute(
                            sa.select(CHANNEL_BINDINGS)
                            .where(
                                sa.or_(
                                    CHANNEL_BINDINGS.c.task_id == command.task_id,
                                    sa.and_(
                                        CHANNEL_BINDINGS.c.tenant_id
                                        == command.tenant_id,
                                        CHANNEL_BINDINGS.c.environment_id
                                        == command.environment_id,
                                        CHANNEL_BINDINGS.c.channel
                                        == command.channel.value,
                                        CHANNEL_BINDINGS.c.source_event_ref
                                        == command.source_event_ref,
                                    ),
                                )
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(rows) != 1:
                    raise ChannelBindingConflictError
                binding = row_to_channel_binding(rows[0])
                if not binding_matches_command(binding, command):
                    raise ChannelBindingConflictError
            else:
                binding = row_to_channel_binding(inserted)
            if command.projection is not None:
                await self._create_subscription(connection, command.projection)
            return binding

    @_persistence_boundary(write=False)
    async def get_group_binding(self, *, lookup: GroupBindingLookup) -> ChannelBinding:
        statement = sa.select(CHANNEL_BINDINGS).where(
            CHANNEL_BINDINGS.c.task_id == lookup.task_id,
            CHANNEL_BINDINGS.c.tenant_id == lookup.tenant_id,
            CHANNEL_BINDINGS.c.environment_id == lookup.environment_id,
            CHANNEL_BINDINGS.c.channel == ChannelKind.FEISHU_GROUP.value,
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            raise ChannelBindingNotFoundError
        return row_to_channel_binding(row)

    @_persistence_boundary(write=False)
    async def get_binding(self, *, lookup: ChannelBindingLookup) -> ChannelBinding:
        statement = sa.select(CHANNEL_BINDINGS).where(
            CHANNEL_BINDINGS.c.task_id == lookup.task_id,
            CHANNEL_BINDINGS.c.tenant_id == lookup.tenant_id,
            CHANNEL_BINDINGS.c.environment_id == lookup.environment_id,
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            raise ChannelBindingNotFoundError
        return row_to_channel_binding(row)

    @_persistence_boundary(write=False)
    async def list_group_bound_task_ids(
        self, *, query: GroupBoundTaskIdsQuery
    ) -> frozenset[str]:
        statement = sa.select(CHANNEL_BINDINGS.c.task_id).where(
            CHANNEL_BINDINGS.c.tenant_id == query.tenant_id,
            CHANNEL_BINDINGS.c.environment_id == query.environment_id,
            CHANNEL_BINDINGS.c.channel == ChannelKind.FEISHU_GROUP.value,
            CHANNEL_BINDINGS.c.task_id.in_(query.task_ids),
        )
        async with self._engine.connect() as connection:
            task_ids = (await connection.execute(statement)).scalars().all()
        return frozenset(task_ids)

    @_persistence_boundary(write=True)
    async def create_projection_subscription(
        self, *, command: CreateProjectionSubscriptionCommand
    ) -> ProjectionSubscription:
        async with _write_transaction(self._engine) as connection:
            return await self._create_subscription(connection, command)

    @_persistence_boundary(write=False)
    async def list_due_projection_subscriptions(
        self, *, query: ProjectionDueQuery
    ) -> tuple[ProjectionSubscription, ...]:
        now = self._clock()
        statement = (
            sa.select(PROJECTION_SUBSCRIPTIONS)
            .select_from(
                PROJECTION_SUBSCRIPTIONS.join(
                    CHANNEL_BINDINGS,
                    CHANNEL_BINDINGS.c.task_id == PROJECTION_SUBSCRIPTIONS.c.task_id,
                )
            )
            .where(
                CHANNEL_BINDINGS.c.tenant_id == query.tenant_id,
                CHANNEL_BINDINGS.c.environment_id == query.environment_id,
                PROJECTION_SUBSCRIPTIONS.c.state.not_in(
                    [ProjectionState.COMPLETED.value, ProjectionState.DEAD_LETTER.value]
                ),
                PROJECTION_SUBSCRIPTIONS.c.next_attempt_at <= now,
                sa.or_(
                    PROJECTION_SUBSCRIPTIONS.c.claim_expires_at.is_(None),
                    PROJECTION_SUBSCRIPTIONS.c.claim_expires_at <= now,
                ),
            )
            .order_by(
                PROJECTION_SUBSCRIPTIONS.c.next_attempt_at,
                PROJECTION_SUBSCRIPTIONS.c.subscription_id,
            )
            .limit(query.limit)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return tuple(row_to_projection_subscription(row) for row in rows)

    async def _require_subscription(
        self, connection: AsyncConnection, subscription_id: str
    ) -> ProjectionSubscription:
        row = (
            (
                await connection.execute(
                    sa.select(PROJECTION_SUBSCRIPTIONS)
                    .where(
                        PROJECTION_SUBSCRIPTIONS.c.subscription_id == subscription_id
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ProjectionSubscriptionNotFoundError
        return row_to_projection_subscription(row)

    async def _persist_projection_result(
        self,
        connection: AsyncConnection,
        result: ProjectionUpdateResult,
    ) -> ProjectionUpdateResult:
        if not result.applied:
            return result
        row = (
            (
                await connection.execute(
                    sa.update(PROJECTION_SUBSCRIPTIONS)
                    .where(
                        PROJECTION_SUBSCRIPTIONS.c.subscription_id
                        == result.winner.subscription_id
                    )
                    .values(**projection_subscription_to_row(result.winner))
                    .returning(PROJECTION_SUBSCRIPTIONS)
                )
            )
            .mappings()
            .one()
        )
        return ProjectionUpdateResult(
            applied=True, winner=row_to_projection_subscription(row)
        )

    @_persistence_boundary(write=True)
    async def claim_projection_subscription(
        self, *, command: ClaimProjectionCommand
    ) -> ProjectionUpdateResult:
        async with _write_transaction(self._engine) as connection:
            current = await self._require_subscription(
                connection, command.subscription_id
            )
            preliminary = claim_projection(
                current, command, now=self._clock(), fencing_token=1
            )
            if not preliminary.applied:
                return preliminary
            token = (
                await connection.execute(
                    sa.select(PROJECTION_FENCING_SEQUENCE.next_value())
                )
            ).scalar_one()
            result = claim_projection(
                current, command, now=self._clock(), fencing_token=token
            )
            return await self._persist_projection_result(connection, result)

    @_persistence_boundary(write=False)
    async def resolve_claimed_task_lookup(
        self, *, lookup: ClaimedTaskLookup
    ) -> TaskLookup:
        async with self._engine.connect() as connection:
            subscription_row = (
                (
                    await connection.execute(
                        sa.select(PROJECTION_SUBSCRIPTIONS).where(
                            PROJECTION_SUBSCRIPTIONS.c.subscription_id
                            == lookup.subscription_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if subscription_row is None:
                raise ProjectionClaimNotFoundError
            subscription = row_to_projection_subscription(subscription_row)
            binding_row = (
                (
                    await connection.execute(
                        sa.select(CHANNEL_BINDINGS).where(
                            CHANNEL_BINDINGS.c.task_id == subscription.task_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        binding = None if binding_row is None else row_to_channel_binding(binding_row)
        return authorize_claimed_task_lookup(
            subscription,
            binding,
            lookup,
            now=self._clock(),
        )

    async def _mutate_projection(
        self,
        *,
        command: ProjectionClaimMutation,
        decision: Callable[..., ProjectionUpdateResult],
    ) -> ProjectionUpdateResult:
        async with _write_transaction(self._engine) as connection:
            current = await self._require_subscription(
                connection, command.subscription_id
            )
            result = decision(current, command, now=self._clock())
            return await self._persist_projection_result(connection, result)

    @_persistence_boundary(write=True)
    async def renew_projection_claim(
        self, *, command: RenewProjectionClaimCommand
    ) -> ProjectionUpdateResult:
        return await self._mutate_projection(
            command=command, decision=renew_projection_claim
        )

    @_persistence_boundary(write=True)
    async def record_initial_projection(
        self, *, command: RecordInitialProjectionCommand
    ) -> ProjectionUpdateResult:
        return await self._mutate_projection(
            command=command, decision=record_initial_projection
        )

    @_persistence_boundary(write=True)
    async def schedule_task_recheck(
        self, *, command: ScheduleTaskRecheckCommand
    ) -> ProjectionUpdateResult:
        return await self._mutate_projection(
            command=command, decision=schedule_task_recheck
        )

    @_persistence_boundary(write=True)
    async def schedule_provider_retry(
        self, *, command: ScheduleProviderRetryCommand
    ) -> ProjectionUpdateResult:
        return await self._mutate_projection(
            command=command, decision=schedule_provider_retry
        )

    @_persistence_boundary(write=True)
    async def complete_projection(
        self, *, command: CompleteProjectionCommand
    ) -> ProjectionUpdateResult:
        return await self._mutate_projection(
            command=command, decision=complete_projection
        )

    @_persistence_boundary(write=True)
    async def dead_letter_projection(
        self, *, command: DeadLetterProjectionCommand
    ) -> ProjectionUpdateResult:
        return await self._mutate_projection(
            command=command, decision=dead_letter_projection
        )


class PostgresWebSessionStore:
    """WebSessionStore 的 PostgreSQL 实现；消费与轮换均由事务裁决。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        clock: Clock,
        oauth_state_capacity: int = DEFAULT_OAUTH_STATE_CAPACITY,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._oauth_state_capacity = validate_oauth_state_capacity(
            oauth_state_capacity
        )

    @_persistence_boundary(write=True)
    async def issue_oauth_state(
        self, *, command: IssueOAuthStateCommand
    ) -> OAuthState:
        conflict_reached = False
        capacity_reached = False
        issued: OAuthState | None = None
        async with _write_transaction(self._engine) as connection:
            await connection.execute(_OAUTH_STATE_CAPACITY_LOCK)
            now = self._clock()
            await connection.execute(
                sa.delete(WEB_OAUTH_STATES).where(
                    sa.or_(
                        WEB_OAUTH_STATES.c.consumed_at.is_not(None),
                        WEB_OAUTH_STATES.c.expires_at <= now,
                    )
                )
            )
            live_digest = await connection.scalar(
                sa.select(WEB_OAUTH_STATES.c.state_digest).where(
                    WEB_OAUTH_STATES.c.state_digest == command.state_digest,
                    WEB_OAUTH_STATES.c.consumed_at.is_(None),
                    WEB_OAUTH_STATES.c.expires_at > now,
                )
            )
            if live_digest is not None:
                conflict_reached = True
            else:
                pending = await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(WEB_OAUTH_STATES)
                    .where(
                        WEB_OAUTH_STATES.c.consumed_at.is_(None),
                        WEB_OAUTH_STATES.c.expires_at > now,
                    )
                )
                if int(pending or 0) >= self._oauth_state_capacity:
                    capacity_reached = True
                else:
                    candidate = OAuthState(
                        state_digest=command.state_digest,
                        issued_at=now,
                        expires_at=now
                        + _dt.timedelta(seconds=command.ttl_seconds),
                    )
                    row = (
                        (
                            await connection.execute(
                                sa.dialects.postgresql.insert(WEB_OAUTH_STATES)
                                .values(**oauth_state_to_row(candidate))
                                .on_conflict_do_nothing()
                                .returning(WEB_OAUTH_STATES)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        conflict_reached = True
                    else:
                        issued = row_to_oauth_state(row)
        if conflict_reached:
            raise WebSessionConflictError
        if capacity_reached:
            raise OAuthStateCapacityError
        if issued is None:
            raise RuntimeError("oauth state issue result missing")
        return issued

    @_persistence_boundary(write=True)
    async def consume_oauth_state(
        self, *, command: ConsumeOAuthStateCommand
    ) -> OAuthState:
        now = self._clock()
        async with _write_transaction(self._engine) as connection:
            row = (
                (
                    await connection.execute(
                        sa.update(WEB_OAUTH_STATES)
                        .where(
                            WEB_OAUTH_STATES.c.state_digest == command.state_digest,
                            WEB_OAUTH_STATES.c.consumed_at.is_(None),
                            WEB_OAUTH_STATES.c.expires_at > now,
                        )
                        .values(consumed_at=now)
                        .returning(WEB_OAUTH_STATES)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise OAuthStateNotFoundError
            return row_to_oauth_state(row)

    @_persistence_boundary(write=True)
    async def rotate_session(
        self, *, command: RotateWebSessionCommand
    ) -> WebSession:
        now = self._clock()
        candidate = WebSession(
            session_digest=command.session_digest,
            subject_ref=command.subject_ref,
            issued_at=now,
            expires_at=now + _dt.timedelta(seconds=command.ttl_seconds),
            auth_source=command.auth_source,
            public_origin_digest=command.public_origin_digest,
        )
        async with _write_transaction(self._engine) as connection:
            row = (
                (
                    await connection.execute(
                        sa.dialects.postgresql.insert(WEB_SESSIONS)
                        .values(**web_session_to_row(candidate))
                        .on_conflict_do_nothing()
                        .returning(WEB_SESSIONS)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise WebSessionConflictError
            previous_digest = command.previous_session_digest
            if previous_digest is not None:
                await connection.execute(
                    sa.update(WEB_SESSIONS)
                    .where(
                        WEB_SESSIONS.c.session_digest == previous_digest,
                        WEB_SESSIONS.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            return row_to_web_session(row)

    @_persistence_boundary(write=False)
    async def get_session(self, *, lookup: WebSessionLookup) -> WebSession:
        now = self._clock()
        statement = sa.select(WEB_SESSIONS).where(
            WEB_SESSIONS.c.session_digest == lookup.session_digest,
            WEB_SESSIONS.c.revoked_at.is_(None),
            WEB_SESSIONS.c.expires_at > now,
            # origin 绑定放在查询条件里：调用方不可能忘记比对。
            WEB_SESSIONS.c.public_origin_digest == lookup.public_origin_digest,
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            raise WebSessionNotFoundError
        return row_to_web_session(row)

    @_persistence_boundary(write=True)
    async def revoke_session(self, *, command: RevokeWebSessionCommand) -> bool:
        async with _write_transaction(self._engine) as connection:
            digest = await connection.scalar(
                sa.update(WEB_SESSIONS)
                .where(
                    WEB_SESSIONS.c.session_digest == command.session_digest,
                    WEB_SESSIONS.c.revoked_at.is_(None),
                )
                .values(revoked_at=self._clock())
                .returning(WEB_SESSIONS.c.session_digest)
            )
        return digest is not None


class PostgresTaskStore:
    def __init__(
        self, *, engine: AsyncEngine, clock: Clock, task_failure_limit: int = 3
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._task_failure_limit = validate_task_failure_limit(task_failure_limit)

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

    @_persistence_boundary(write=False)
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

    def _stored_read_from_joined_row(self, row: Mapping[str, Any]) -> StoredTaskRead:
        record = row_to_record(_as_row(row))
        try:
            submission = TaskSubmission(
                envelope=load_contract(RequestEnvelope, row["submission_envelope"]),
                context=load_contract(RequestContext, row["submission_context"]),
                as_of=row["submission_as_of"],
                clarification_parent_task_id=row["submission_clarification_parent_task_id"],
            )
        except ValueError as exc:
            raise TaskNotFoundError(task_id=record.task_id) from exc
        if not submission_matches_record(
            record,
            submission,
            stored_digest=row["submission_digest"],
        ):
            raise TaskNotFoundError(task_id=record.task_id)
        return StoredTaskRead(record=record, submission=submission)

    def _task_page_statement(
        self,
        *,
        tenant_id: str,
        environment_id: str,
        actor: str | None,
        before_created_seq: int | None,
        limit: int,
    ) -> sa.sql.Select[tuple[Any, ...]]:
        conditions = [
            TASKS.c.tenant_id == tenant_id,
            TASKS.c.environment_id == environment_id,
        ]
        if actor is not None:
            conditions.append(TASKS.c.actor == actor)
        if before_created_seq is not None:
            conditions.append(TASKS.c.created_seq < before_created_seq)
        return (
            sa.select(
                TASKS,
                TASK_SUBMISSIONS.c.envelope.label("submission_envelope"),
                TASK_SUBMISSIONS.c.context.label("submission_context"),
                TASK_SUBMISSIONS.c.as_of.label("submission_as_of"),
                TASK_SUBMISSIONS.c.submission_digest.label("submission_digest"),
                TASK_SUBMISSIONS.c.clarification_parent_task_id.label("submission_clarification_parent_task_id"),
            )
            .join(TASK_SUBMISSIONS, TASK_SUBMISSIONS.c.task_id == TASKS.c.task_id)
            .where(*conditions)
            .order_by(TASKS.c.created_seq.desc(), TASKS.c.task_id.desc())
            .limit(limit + 1)
        )

    async def _list_task_page(
        self,
        *,
        tenant_id: str,
        environment_id: str,
        actor: str | None,
        before_created_seq: int | None,
        limit: int,
    ) -> StoredTaskPage:
        statement = self._task_page_statement(
            tenant_id=tenant_id,
            environment_id=environment_id,
            actor=actor,
            before_created_seq=before_created_seq,
            limit=limit,
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        selected = rows[:limit]
        items = tuple(self._stored_read_from_joined_row(row) for row in selected)
        next_created_seq = (
            items[-1].record.created_seq if len(rows) > limit else None
        )
        return StoredTaskPage(items=items, next_created_seq=next_created_seq)

    @_persistence_boundary(write=False)
    async def get_submission(self, *, lookup: TaskLookup) -> TaskSubmission:
        statement = (
            self._task_page_statement(
                tenant_id=lookup.tenant_id,
                environment_id=lookup.environment_id,
                actor=None,
                before_created_seq=None,
                limit=1,
            )
            .where(TASKS.c.task_id == lookup.task_id)
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            raise TaskNotFoundError(task_id=lookup.task_id)
        return self._stored_read_from_joined_row(row).submission

    @_persistence_boundary(write=False)
    async def list_tasks_for_actor(
        self, *, query: ActorTaskPageQuery
    ) -> StoredTaskPage:
        return await self._list_task_page(
            tenant_id=query.tenant_id,
            environment_id=query.environment_id,
            actor=query.actor,
            before_created_seq=query.before_created_seq,
            limit=query.limit,
        )

    @_persistence_boundary(write=False)
    async def list_tasks_for_scope(
        self, *, query: ScopeTaskPageQuery
    ) -> StoredTaskPage:
        return await self._list_task_page(
            tenant_id=query.tenant_id,
            environment_id=query.environment_id,
            actor=None,
            before_created_seq=query.before_created_seq,
            limit=query.limit,
        )

    @_persistence_boundary(write=False)
    async def list_stale_leases(
        self, *, query: StaleLeaseQuery
    ) -> tuple[TaskRecord, ...]:
        """曾被租出、租约已过期、未终态。**不加锁、不改状态。**

        SQL 谓词与 ``is_stale_lease`` 逐条等价，理由见 :func:`stale_lease_statement`。
        Python 侧仍再过滤一次：那是两个实现共用的权威判定，"什么算 stale"只能有一个
        答案。等价之后这一步在正常数据上是空操作，但它是 SQL 一旦漂移时的兜底。
        """
        now = self._clock()
        statement = stale_lease_statement(now=now, limit=query.limit)
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        records = [row_to_record(_as_row(row)) for row in rows]
        stale = [record for record in records if is_stale_lease(record, now=now)]
        return tuple(sorted(stale, key=stale_lease_sort_key))

    @_persistence_boundary(write=False)
    async def list_dispatchable_tasks(
        self, *, query: DispatchQuery
    ) -> tuple[TaskRecord, ...]:
        now = self._clock()
        live_lease = sa.and_(
            TASKS.c.lease_owner.is_not(None),
            TASKS.c.lease_expires_at.is_not(None),
            TASKS.c.lease_expires_at > now,
        )
        statement = (
            sa.select(TASKS)
            .where(
                TASKS.c.tenant_id == query.tenant_id,
                TASKS.c.environment_id == query.environment_id,
                TASKS.c.status.in_(
                    sorted(
                        status.value
                        for status in attempt_statuses(AttemptIntent.DISPATCH)
                    )
                ),
                sa.or_(TASKS.c.next_attempt_at.is_(None), TASKS.c.next_attempt_at <= now),
                sa.not_(live_lease),
            )
            .order_by(TASKS.c.created_seq, TASKS.c.task_id)
            .limit(query.limit)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        records = [row_to_record(_as_row(row)) for row in rows]
        return tuple(
            sorted(
                (
                    record
                    for record in records
                    if is_dispatchable(record, query, now=now)
                ),
                key=dispatch_sort_key,
            )
        )

    async def _load_submission_row(
        self, connection: AsyncConnection, task_id: str
    ) -> Mapping[str, Any] | None:
        found = (
            (
                await connection.execute(
                    sa.select(TASK_SUBMISSIONS).where(
                        TASK_SUBMISSIONS.c.task_id == task_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            return None
        return {column.name: found[column.name] for column in TASK_SUBMISSIONS.columns}

    async def _terminalize_attempt_rejection(
        self,
        connection: AsyncConnection,
        *,
        current: TaskRecord,
        command: TaskAttemptCommand,
        rejection: TaskAttemptRejection,
        now: _dt.datetime,
    ) -> TaskAttemptResult:
        expected = current.model_copy(
            update={
                "status": TaskStatus.FAILED,
                "version": current.version + 1,
                "terminal_reason": rejection.value,
            }
        )
        row = (
            (
                await connection.execute(
                    sa.update(TASKS)
                    .where(TASKS.c.task_id == current.task_id)
                    .values(
                        status=expected.status.value,
                        version=expected.version,
                        terminal_reason=expected.terminal_reason,
                    )
                    .returning(TASKS)
                )
            )
            .mappings()
            .one()
        )
        winner = row_to_record(_as_row(row))
        event = attempt_terminal_event(command=command, winner=winner, now=now)
        await _append_audit_events(
            connection, task_id=current.task_id, events=(event,)
        )
        return TaskAttemptResult(
            applied=False,
            winner=winner,
            rejection=rejection,
            committed_audit_events=(event,),
        )

    @_persistence_boundary(write=True)
    async def begin_task_attempt(
        self, *, command: TaskAttemptCommand
    ) -> TaskAttemptResult:
        async with _write_transaction(self._engine) as connection:
            current = row_to_record(
                await self._require_row(connection, command.task_id, for_update=True)
            )
            now = self._clock()
            rejection = classify_task_attempt(
                current,
                intent=command.intent,
                now=now,
                task_failure_limit=self._task_failure_limit,
            )
            if rejection is TaskAttemptRejection.RETRY_EXHAUSTED:
                return await self._terminalize_attempt_rejection(
                    connection,
                    current=current,
                    command=command,
                    rejection=rejection,
                    now=now,
                )
            if rejection is not None:
                return TaskAttemptResult(
                    applied=False, winner=current, rejection=rejection
                )

            stored = await self._load_submission_row(connection, current.task_id)
            submission: TaskSubmission | None = None
            if stored is not None:
                try:
                    submission = TaskSubmission(
                        envelope=load_contract(RequestEnvelope, stored["envelope"]),
                        context=load_contract(RequestContext, stored["context"]),
                        as_of=stored["as_of"],
                        clarification_parent_task_id=stored["clarification_parent_task_id"],
                    )
                except ValueError:
                    submission = None
            if (
                stored is None
                or submission is None
                or not submission_matches_record(
                    current,
                    submission,
                    stored_digest=stored["submission_digest"],
                )
            ):
                return await self._terminalize_attempt_rejection(
                    connection,
                    current=current,
                    command=command,
                    rejection=TaskAttemptRejection.SUBMISSION_INVARIANT_VIOLATION,
                    now=now,
                )

            token = (
                await connection.execute(sa.select(FENCING_SEQUENCE.next_value()))
            ).scalar_one()
            expires_at = now + _dt.timedelta(seconds=command.ttl_seconds)
            row = (
                (
                    await connection.execute(
                        sa.update(TASKS)
                        .where(TASKS.c.task_id == current.task_id)
                        .values(
                            attempt_number=current.attempt_number + 1,
                            next_attempt_at=None,
                            lease_owner=command.owner,
                            lease_expires_at=expires_at,
                            fencing_token=token,
                        )
                        .returning(TASKS)
                    )
                )
                .mappings()
                .one()
            )
            winner = row_to_record(_as_row(row))
            grant = TaskAttemptGrant(
                lease=LeaseGrant(
                    task_id=current.task_id,
                    owner=command.owner,
                    expires_at=expires_at,
                    fencing_token=token,
                ),
                attempt_number=winner.attempt_number,
            )
            return TaskAttemptResult(
                applied=True,
                winner=winner,
                grant=grant,
                submission=submission,
            )

    @_persistence_boundary(write=True)
    async def schedule_retry(self, *, command: RetryCommand) -> RetryResult:
        digest = retry_command_digest(command)
        async with _write_transaction(self._engine) as connection:
            current = row_to_record(
                await self._require_row(
                    connection, command.grant.task_id, for_update=True
                )
            )
            if current.status in TERMINAL_STATUSES:
                return RetryResult(
                    decision=RetryDecision.TERMINAL_PROTECTED, winner=current
                )
            if (
                current.retry_scheduled_by_attempt == command.grant.attempt_number
                and current.attempt_number == command.grant.attempt_number
            ):
                decision = (
                    RetryDecision.ALREADY_SCHEDULED
                    if current.retry_command_digest == digest
                    else RetryDecision.COMMAND_MISMATCH
                )
                return RetryResult(decision=decision, winner=current)

            rejection = grant_is_current(
                current,
                command.grant,
                now=self._clock(),
                allowed_statuses=attempt_statuses(AttemptIntent.DISPATCH),
            )
            if rejection is not None:
                return RetryResult(decision=RetryDecision.STALE_FENCING, winner=current)

            token = (
                await connection.execute(sa.select(FENCING_SEQUENCE.next_value()))
            ).scalar_one()
            row = (
                (
                    await connection.execute(
                        sa.update(TASKS)
                        .where(TASKS.c.task_id == current.task_id)
                        .values(
                            version=current.version + 1,
                            task_failure_count=current.task_failure_count + 1,
                            next_attempt_at=command.next_attempt_at,
                            retry_scheduled_by_attempt=command.grant.attempt_number,
                            retry_command_digest=digest,
                            lease_expires_at=command.next_attempt_at,
                            fencing_token=token,
                        )
                        .returning(TASKS)
                    )
                )
                .mappings()
                .one()
            )
            await _append_audit_events(
                connection,
                task_id=current.task_id,
                events=command.audit_events,
            )
            return RetryResult(
                decision=RetryDecision.SCHEDULED,
                winner=row_to_record(_as_row(row)),
            )

    async def _load_step_row(
        self, connection: AsyncConnection, *, task_id: str, step_id: str
    ) -> StepExecutionRecord | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_STEP_EXECUTIONS).where(
                        TASK_STEP_EXECUTIONS.c.task_id == task_id,
                        TASK_STEP_EXECUTIONS.c.step_id == step_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else row_to_step_execution(row)

    async def _load_execution_plan(
        self, connection: AsyncConnection, *, task_id: str
    ) -> ExecutionPlan | None:
        payload = (
            await connection.execute(
                sa.select(TASK_PLANS.c.plan).where(TASK_PLANS.c.task_id == task_id)
            )
        ).scalar_one_or_none()
        if payload is None:
            return None
        reject_unsupported_plan_schema(payload, task_id=task_id)
        return load_contract(ExecutionPlan, payload)

    async def _step_attempts_used(
        self, connection: AsyncConnection, *, task_id: str
    ) -> int:
        used = (
            await connection.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(TASK_STEP_EXECUTIONS.c.attempt_count), 0)
                ).where(TASK_STEP_EXECUTIONS.c.task_id == task_id)
            )
        ).scalar_one()
        return int(used)

    @_persistence_boundary(write=True)
    async def begin_step_attempt(
        self, *, command: StepAttemptCommand
    ) -> StepAttemptResult:
        async with _write_transaction(self._engine) as connection:
            current = row_to_record(
                await self._require_row(
                    connection, command.grant.task_id, for_update=True
                )
            )
            existing = await self._load_step_row(
                connection, task_id=current.task_id, step_id=command.step_id
            )
            attempts_used = await self._step_attempts_used(
                connection, task_id=current.task_id
            )
            rejection = grant_is_current(
                current,
                command.grant,
                now=self._clock(),
                allowed_statuses=frozenset({TaskStatus.RUNNING}),
            )
            if rejection is not None:
                decision = (
                    StepAttemptDecision.NOT_RUNNABLE
                    if rejection
                    in {
                        GrantRejection.TERMINAL_PROTECTED,
                        GrantRejection.STATUS_NOT_ALLOWED,
                    }
                    else StepAttemptDecision.STALE_FENCING
                )
                return StepAttemptResult(
                    decision=decision,
                    winner=current,
                    record=existing,
                    attempts_used=attempts_used,
                )
            if existing is not None and existing.result_status is not None:
                return StepAttemptResult(
                    decision=StepAttemptDecision.ALREADY_COMMITTED,
                    winner=current,
                    record=existing,
                    attempts_used=attempts_used,
                )

            plan = await self._load_execution_plan(
                connection, task_id=current.task_id
            )
            if plan is None:
                return StepAttemptResult(
                    decision=StepAttemptDecision.NOT_RUNNABLE,
                    winner=current,
                    record=existing,
                    attempts_used=attempts_used,
                )
            if command.step_id not in {step.step_id for step in plan.steps}:
                return StepAttemptResult(
                    decision=StepAttemptDecision.UNKNOWN_STEP,
                    winner=current,
                    record=None,
                    attempts_used=attempts_used,
                )

            increments_budget = (
                existing is None
                or existing.last_fencing_token != command.grant.fencing_token
            )
            if increments_budget and attempts_used >= plan.budget.max_tool_calls:
                return StepAttemptResult(
                    decision=StepAttemptDecision.BUDGET_EXHAUSTED,
                    winner=current,
                    record=existing,
                    attempts_used=attempts_used,
                )

            if existing is None:
                candidate = StepExecutionRecord(
                    task_id=current.task_id,
                    step_id=command.step_id,
                    attempt_count=1,
                    last_fencing_token=command.grant.fencing_token,
                    started_at=self._clock(),
                )
                row = (
                    (
                        await connection.execute(
                            sa.insert(TASK_STEP_EXECUTIONS)
                            .values(**step_execution_to_row(candidate))
                            .returning(TASK_STEP_EXECUTIONS)
                        )
                    )
                    .mappings()
                    .one()
                )
                record = row_to_step_execution(row)
            elif increments_budget:
                row = (
                    (
                        await connection.execute(
                            sa.update(TASK_STEP_EXECUTIONS)
                            .where(
                                TASK_STEP_EXECUTIONS.c.task_id == current.task_id,
                                TASK_STEP_EXECUTIONS.c.step_id == command.step_id,
                            )
                            .values(
                                attempt_count=existing.attempt_count + 1,
                                last_fencing_token=command.grant.fencing_token,
                                started_at=self._clock(),
                            )
                            .returning(TASK_STEP_EXECUTIONS)
                        )
                    )
                    .mappings()
                    .one()
                )
                record = row_to_step_execution(row)
            else:
                record = existing
            return StepAttemptResult(
                decision=StepAttemptDecision.PROCEED,
                winner=current,
                record=record,
                attempts_used=await self._step_attempts_used(
                    connection, task_id=current.task_id
                ),
            )

    @_persistence_boundary(write=True)
    async def commit_step_result(
        self, *, command: StepCommitCommand
    ) -> StepCommitResult:
        digest = step_commit_digest(command)
        async with _write_transaction(self._engine) as connection:
            current = row_to_record(
                await self._require_row(
                    connection, command.grant.task_id, for_update=True
                )
            )
            existing = await self._load_step_row(
                connection, task_id=current.task_id, step_id=command.step_id
            )
            rejection = grant_is_current(
                current,
                command.grant,
                now=self._clock(),
                allowed_statuses=frozenset({TaskStatus.RUNNING}),
            )
            if rejection is not None:
                decision = (
                    StepCommitRejection.NOT_RUNNABLE
                    if rejection
                    in {
                        GrantRejection.TERMINAL_PROTECTED,
                        GrantRejection.STATUS_NOT_ALLOWED,
                    }
                    else StepCommitRejection.STALE_FENCING
                )
                return StepCommitResult(
                    committed=False,
                    winner=current,
                    record=existing,
                    rejection=decision,
                )
            if (
                existing is None
                or existing.last_fencing_token != command.grant.fencing_token
            ):
                return StepCommitResult(
                    committed=False,
                    winner=current,
                    rejection=StepCommitRejection.NO_ATTEMPT_IN_FLIGHT,
                )
            if existing.result_status is not None:
                if existing.commit_digest == digest:
                    return StepCommitResult(
                        committed=True, winner=current, record=existing
                    )
                return StepCommitResult(
                    committed=False,
                    winner=current,
                    record=existing,
                    rejection=StepCommitRejection.ALREADY_COMMITTED_DIFFERENT,
                )

            plan = await self._load_execution_plan(
                connection, task_id=current.task_id
            )
            if plan is None:
                raise ValueError("step commit has no stored plan")
            evidence = command.evidence
            if evidence is not None and (
                evidence.capability_id != plan.capability_id
                or evidence.capability_version != plan.capability_version
            ):
                raise ValueError("step evidence does not match the stored plan")

            if evidence is not None:
                await _append_evidence(
                    connection, task_id=current.task_id, envelope=evidence
                )
            committed_at = self._clock()
            row = (
                (
                    await connection.execute(
                        sa.update(TASK_STEP_EXECUTIONS)
                        .where(
                            TASK_STEP_EXECUTIONS.c.task_id == current.task_id,
                            TASK_STEP_EXECUTIONS.c.step_id == command.step_id,
                            TASK_STEP_EXECUTIONS.c.result_status.is_(None),
                        )
                        .values(
                            result_status=command.status.value,
                            kind=command.kind.value,
                            evidence_id=None
                            if evidence is None
                            else evidence.evidence_id,
                            commit_digest=digest,
                            committed_at=committed_at,
                        )
                        .returning(TASK_STEP_EXECUTIONS)
                    )
                )
                .mappings()
                .one()
            )
            await _append_audit_events(
                connection,
                task_id=current.task_id,
                events=command.audit_events,
            )
            return StepCommitResult(
                committed=True,
                winner=current,
                record=row_to_step_execution(row),
            )

    @_persistence_boundary(write=False)
    async def load_step_executions(
        self, *, task_id: str
    ) -> tuple[StepExecutionRecord, ...]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.select(TASK_STEP_EXECUTIONS)
                        .where(TASK_STEP_EXECUTIONS.c.task_id == task_id)
                        .order_by(TASK_STEP_EXECUTIONS.c.step_id)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(row_to_step_execution(row) for row in rows)

    # --- 写 -------------------------------------------------------------------

    async def _load_existing_for_scope(
        self,
        connection: AsyncConnection,
        *,
        scope_digest: str,
    ) -> Mapping[str, Any]:
        return _as_row(
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

    def _record_or_conflict(
        self,
        *,
        row: Mapping[str, Any],
        request_context: RequestContext,
        envelope: RequestEnvelope,
        digest: str,
    ) -> TaskRecord:
        record = row_to_record(_as_row(row))
        same_scope = (
            record.tenant_id == request_context.tenant_id
            and record.environment_id == request_context.environment_id
            and record.idempotency_key == envelope.idempotency_key
        )
        if not same_scope or record.request_digest != digest:
            raise IdempotencyConflictError("idempotency key reused for a different request")
        return record

    async def _existing_record_for_submission(
        self,
        connection: AsyncConnection,
        *,
        scope_digest: str,
        request_context: RequestContext,
        envelope: RequestEnvelope,
        digest: str,
    ) -> TaskRecord | None:
        found = (
            (
                await connection.execute(
                    sa.select(TASKS).where(
                        TASKS.c.idempotency_scope_digest == scope_digest
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            return None
        return self._record_or_conflict(
            row=_as_row(found),
            request_context=request_context,
            envelope=envelope,
            digest=digest,
        )

    async def _insert_task_with_submission(
        self,
        connection: AsyncConnection,
        *,
        submission: TaskSubmission,
        digest: str,
        scope_digest: str,
    ) -> TaskRecord | None:
        envelope = submission.envelope
        request_context = submission.context
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
        if inserted is None:
            return None
        await connection.execute(
            sa.insert(TASK_SUBMISSIONS).values(
                task_id=candidate.task_id,
                envelope=dump_contract(submission.envelope),
                context=dump_contract(submission.context),
                as_of=submission.as_of,
                submission_digest=submission_digest(submission),
                clarification_parent_task_id=submission.clarification_parent_task_id,
            )
        )
        return row_to_record(_as_row(inserted))

    @_persistence_boundary(write=True)
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
        reject_clarification_parent_on_plain_create(submission)
        digest = request_dedup_digest(
            envelope,
            request_context,
            clarification_parent_task_id=submission.clarification_parent_task_id,
        )
        scope_digest = idempotency_scope_digest(
            tenant_id=request_context.tenant_id,
            environment_id=request_context.environment_id,
            idempotency_key=envelope.idempotency_key,
        )
        async with _write_transaction(self._engine) as connection:
            inserted = await self._insert_task_with_submission(
                connection,
                submission=submission,
                digest=digest,
                scope_digest=scope_digest,
            )
            if inserted is not None:
                return inserted
            existing = await self._load_existing_for_scope(
                connection, scope_digest=scope_digest
            )
        return self._record_or_conflict(
            row=existing,
            request_context=request_context,
            envelope=envelope,
            digest=digest,
        )

    @_persistence_boundary(write=True)
    async def create_clarification_child(
        self,
        *,
        submission: TaskSubmission,
        authenticated_channel_owner: str,
    ) -> TaskRecord:
        clarification_parent_id = submission.clarification_parent_task_id
        if clarification_parent_id is None:
            raise ClarificationParentRequiredError("clarification parent is required")
        envelope = submission.envelope
        request_context = submission.context
        if not context_matches_envelope(envelope, request_context):
            raise ContextMismatchError("envelope and context disagree on execution context")
        digest = request_dedup_digest(
            envelope,
            request_context,
            clarification_parent_task_id=clarification_parent_id,
        )
        scope_digest = idempotency_scope_digest(
            tenant_id=request_context.tenant_id,
            environment_id=request_context.environment_id,
            idempotency_key=envelope.idempotency_key,
        )
        async with _write_transaction(self._engine) as connection:
            existing = await self._existing_record_for_submission(
                connection,
                scope_digest=scope_digest,
                request_context=request_context,
                envelope=envelope,
                digest=digest,
            )
            if existing is not None:
                return existing
            parent_row = await self._select_row(
                connection, clarification_parent_id, for_update=True
            )
            if parent_row is None:
                raise TaskNotFoundError(task_id=clarification_parent_id)
            parent = row_to_record(parent_row)
            existing = await self._existing_record_for_submission(
                connection,
                scope_digest=scope_digest,
                request_context=request_context,
                envelope=envelope,
                digest=digest,
            )
            if existing is not None:
                return existing
            if not clarification_parent_is_usable(
                parent=parent,
                submission=submission,
                authenticated_channel_owner=authenticated_channel_owner,
            ):
                raise TaskNotFoundError(task_id=clarification_parent_id)
            consumed = await connection.scalar(
                sa.select(TASK_SUBMISSIONS.c.task_id)
                .where(
                    TASK_SUBMISSIONS.c.clarification_parent_task_id
                    == clarification_parent_id
                )
                .limit(1)
            )
            if consumed is not None:
                raise TaskNotFoundError(task_id=clarification_parent_id)
            inserted = await self._insert_task_with_submission(
                connection,
                submission=submission,
                digest=digest,
                scope_digest=scope_digest,
            )
            if inserted is not None:
                return inserted
            existing_row = await self._load_existing_for_scope(
                connection, scope_digest=scope_digest
            )
        return self._record_or_conflict(
            row=existing_row,
            request_context=request_context,
            envelope=envelope,
            digest=digest,
        )

    @_persistence_boundary(write=True)
    async def transition(
        self, *, command: TransitionCommand
    ) -> TransitionResult:
        async with _write_transaction(self._engine) as connection:
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
            await _append_audit_events(
                connection,
                task_id=command.task_id,
                events=command.audit_events,
            )
        return TransitionResult(
            applied=True, winner=row_to_record(_as_row(row)), rejection=None
        )

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        command = LeaseCommand(task_id=task_id, owner=owner, ttl_seconds=ttl_seconds)
        return await self._acquire_lease(command=command)

    @_persistence_boundary(write=True)
    async def _acquire_lease(self, *, command: LeaseCommand) -> LeaseGrant | None:
        async with _write_transaction(self._engine) as connection:
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
        return await self._renew_lease(command=command)

    @_persistence_boundary(write=True)
    async def _renew_lease(self, *, command: LeaseCommand) -> LeaseGrant | None:
        token = command.fencing_token if command.fencing_token is not None else 0
        async with _write_transaction(self._engine) as connection:
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

    @_persistence_boundary(write=True)
    async def record_approval(self, *, request: ApprovalRequest) -> int:
        """append-only 写入。``seq`` 由 ``MAX(seq) + 1`` 分配，并发由 advisory lock 串行化。

        ``RETURNING seq`` 取回数据库真正分配到的序号，而不是在 Python 侧再算一遍：
        再算一遍就是空洞检查——两侧同源，比对恒真。
        """
        async with _write_transaction(self._engine) as connection:
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

    @_persistence_boundary(write=True)
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
        async with _write_transaction(self._engine) as connection:
            seqs = await _append_audit_events(
                connection, task_id=task_id, events=(event,)
            )
        return seqs[0]


class PostgresPlanStore:
    """``PlanStore`` 的 PostgreSQL 实现。

    ``save`` 用 ``ON CONFLICT DO NOTHING`` 后比对，而不是"先查后插"：并发的两次
    ``save`` 会双双查不到、双双插入，其中一个撞主键。冲突后读回既存内容再比较，
    内容相同即幂等成功，不同才是 ``PlanConflictError``——重试不该被当成篡改。
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    @_persistence_boundary(write=True)
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
        async with _write_transaction(self._engine) as connection:
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
        reject_unsupported_plan_schema(row["plan"], task_id=task_id)
        return StoredPlan(
            plan=load_contract(ExecutionPlan, row["plan"]),
            target=load_contract(ResolvedTarget, row["target"]),
        )

    @_persistence_boundary(write=False)
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

    @_persistence_boundary(write=True)
    async def append(self, *, task_id: str, envelope: EvidenceEnvelope) -> str:
        async with _write_transaction(self._engine) as connection:
            return await _append_evidence(
                connection, task_id=task_id, envelope=envelope
            )

    @_persistence_boundary(write=False)
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

    @_persistence_boundary(write=False)
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


class PostgresModelArtifactStore:
    """ModelArtifactStore 的 PostgreSQL 实现；任务行锁内验证 grant 并插入。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def _current_for_update(
        self, connection: AsyncConnection, *, task_id: str
    ) -> TaskRecord | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASKS)
                    .where(TASKS.c.task_id == task_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else row_to_record(_as_row(row))

    @staticmethod
    def _require_grant(
        current: TaskRecord | None,
        grant: TaskAttemptGrant,
        *,
        now: _dt.datetime,
        allowed_statuses: frozenset[TaskStatus],
    ) -> None:
        require_model_artifact_grant(
            current,
            grant,
            now=now,
            allowed_statuses=allowed_statuses,
        )

    async def _load_interaction_row(
        self, connection: AsyncConnection, *, task_id: str
    ) -> AcceptedInteractionArtifact | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_INTERACTION_ARTIFACTS).where(
                        TASK_INTERACTION_ARTIFACTS.c.task_id == task_id,
                        TASK_INTERACTION_ARTIFACTS.c.artifact_version == 2,
                    )
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else row_to_interaction_artifact(row)

    @_persistence_boundary(write=False)
    async def load_interaction(
        self, *, task_id: str
    ) -> AcceptedInteractionArtifact | None:
        async with self._engine.connect() as connection:
            return await self._load_interaction_row(connection, task_id=task_id)

    @_persistence_boundary(write=True)
    async def save_interaction(
        self, *, grant: TaskAttemptGrant, candidate: InteractionArtifactCandidate
    ) -> AcceptedInteractionArtifact:
        async with _write_transaction(self._engine) as connection:
            current = await self._current_for_update(
                connection, task_id=grant.task_id
            )
            self._require_grant(
                current,
                grant,
                now=self._clock(),
                allowed_statuses=frozenset(
                    {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}
                ),
            )
            artifact = AcceptedInteractionArtifact(
                **candidate.model_dump(mode="python"),
                task_id=grant.task_id,
                created_at=self._clock(),
                fencing_token=grant.fencing_token,
            )
            insert = (
                sa.dialects.postgresql.insert(TASK_INTERACTION_ARTIFACTS)
                .values(**interaction_artifact_to_row(artifact))
                .on_conflict_do_nothing(index_elements=["task_id"])
                .returning(TASK_INTERACTION_ARTIFACTS.c.task_id)
            )
            if (await connection.execute(insert)).first() is not None:
                return artifact
            existing = await self._load_interaction_row(
                connection, task_id=grant.task_id
            )
            if existing is None:
                legacy = (
                    (
                        await connection.execute(
                            sa.select(
                                TASK_INTERACTION_ARTIFACTS.c.artifact_version
                            )
                            .where(
                                TASK_INTERACTION_ARTIFACTS.c.task_id
                                == grant.task_id
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    legacy is not None
                    and legacy["artifact_version"] == 1
                ):
                    await connection.execute(
                        sa.delete(TASK_INTERACTION_ARTIFACTS).where(
                            TASK_INTERACTION_ARTIFACTS.c.task_id
                            == grant.task_id,
                            TASK_INTERACTION_ARTIFACTS.c.artifact_version == 1,
                        )
                    )
                    if (await connection.execute(insert)).first() is not None:
                        return artifact
                    existing = await self._load_interaction_row(
                        connection, task_id=grant.task_id
                    )
            if existing is None or not artifact_matches_candidate(
                existing, candidate
            ):
                raise ModelArtifactConflictError(
                    "different interaction artifact is already stored",
                    task_id=grant.task_id,
                )
            return existing

    async def _load_advisory_row(
        self, connection: AsyncConnection, *, task_id: str
    ) -> StoredModelAdvisory | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_MODEL_ADVISORIES).where(
                        TASK_MODEL_ADVISORIES.c.task_id == task_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else row_to_model_advisory(row)

    @_persistence_boundary(write=False)
    async def load_advisory(self, *, task_id: str) -> StoredModelAdvisory | None:
        async with self._engine.connect() as connection:
            return await self._load_advisory_row(connection, task_id=task_id)

    @_persistence_boundary(write=True)
    async def save_advisory(
        self, *, grant: TaskAttemptGrant, candidate: AdvisoryArtifactCandidate
    ) -> StoredModelAdvisory:
        async with _write_transaction(self._engine) as connection:
            current = await self._current_for_update(
                connection, task_id=grant.task_id
            )
            self._require_grant(
                current,
                grant,
                now=self._clock(),
                allowed_statuses=frozenset({TaskStatus.RUNNING}),
            )
            artifact = StoredModelAdvisory(
                **candidate.model_dump(mode="python"),
                task_id=grant.task_id,
                created_at=self._clock(),
                fencing_token=grant.fencing_token,
            )
            insert = (
                sa.dialects.postgresql.insert(TASK_MODEL_ADVISORIES)
                .values(**model_advisory_to_row(artifact))
                .on_conflict_do_nothing(index_elements=["task_id"])
                .returning(TASK_MODEL_ADVISORIES.c.task_id)
            )
            if (await connection.execute(insert)).first() is not None:
                return artifact
            existing = await self._load_advisory_row(
                connection, task_id=grant.task_id
            )
            if existing is None or not artifact_matches_candidate(
                existing, candidate
            ):
                raise ModelArtifactConflictError(
                    "different model advisory is already stored",
                    task_id=grant.task_id,
                )
            return existing


class PostgresClarificationRecordStore:
    """ClarificationRecordStore 的 PostgreSQL 实现；任务行锁内验证 grant 并插入。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def _current_for_update(
        self, connection: AsyncConnection, *, task_id: str
    ) -> TaskRecord | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASKS)
                    .where(TASKS.c.task_id == task_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else row_to_record(_as_row(row))

    async def _load_row(
        self, connection: AsyncConnection, *, task_id: str
    ) -> ClarificationRecord | None:
        row = (
            (
                await connection.execute(
                    sa.select(TASK_CLARIFICATION_RECORDS).where(
                        TASK_CLARIFICATION_RECORDS.c.task_id == task_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else row_to_clarification_record(row)

    @_persistence_boundary(write=False)
    async def load(self, *, task_id: str) -> ClarificationRecord | None:
        async with self._engine.connect() as connection:
            return await self._load_row(connection, task_id=task_id)

    @_persistence_boundary(write=True)
    async def save(
        self, *, grant: TaskAttemptGrant, candidate: ClarificationRecordCandidate
    ) -> ClarificationRecord:
        async with _write_transaction(self._engine) as connection:
            current = await self._current_for_update(connection, task_id=grant.task_id)
            require_clarification_record_grant(
                current,
                grant,
                now=self._clock(),
            )
            record = ClarificationRecord(
                **candidate.model_dump(mode="python"),
                task_id=grant.task_id,
                created_at=self._clock(),
                fencing_token=grant.fencing_token,
            )
            insert = (
                sa.dialects.postgresql.insert(TASK_CLARIFICATION_RECORDS)
                .values(**clarification_record_to_row(record))
                .on_conflict_do_nothing(index_elements=["task_id"])
                .returning(TASK_CLARIFICATION_RECORDS.c.task_id)
            )
            if (await connection.execute(insert)).first() is not None:
                return record
            existing = await self._load_row(connection, task_id=grant.task_id)
            if existing is None or not record_matches_candidate(existing, candidate):
                raise ClarificationRecordConflictError(
                    "different clarification record is already stored",
                    task_id=grant.task_id,
                )
            return existing


BOOTSTRAP_LOCAL_ADMIN_LOCK_KEY: Final[int] = 2026092101
"""bootstrap advisory lock 的键。

公开导出，好让并发用例去**真的抢这把锁**而不是在测试里抄一遍字面量——抄出来的那
一份不会跟着这里改，于是有朝一日换了键值，用例会去抢一把没人用的锁并安静地通过。
"""

_BOOTSTRAP_LOCAL_ADMIN_LOCK: Final = sa.text(
    "SELECT pg_advisory_xact_lock(:lock_key)"
).bindparams(lock_key=BOOTSTRAP_LOCAL_ADMIN_LOCK_KEY)
"""bootstrap 的事务级 advisory lock。

比 ``SELECT ... FOR UPDATE`` 严格强一档，而且强的正是需要的那一档：凭据行还不存在
时 ``FOR UPDATE`` 一行都锁不到，两个同时装配的 Web 进程会双双读到"没有这一行"、
双双去建账号，第二个撞 ``user_accounts`` 主键——一次正常的并发启动变成一次启动失败。
拿到锁之后仍然 ``FOR UPDATE`` 那一行，因为四态判定读的就是它。
"""


class PostgresActivationStore:
    """激活申请的 PostgreSQL 实现；容量、收割与判重共用一个事务临界区。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    @_persistence_boundary(write=True)
    async def create_or_reuse(
        self, *, command: CreateActivationCommand
    ) -> ActivationRequest:
        async with _write_transaction(self._engine) as connection:
            await connection.execute(_ACTIVATION_CAPACITY_LOCK)
            now = self._clock()
            await connection.execute(
                sa.update(ACTIVATION_REQUESTS)
                .where(
                    ACTIVATION_REQUESTS.c.status
                    == ActivationStatus.PENDING.value,
                    ACTIVATION_REQUESTS.c.expires_at <= now,
                )
                .values(status=ActivationStatus.EXPIRED.value)
            )
            pending = int(
                await connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(ACTIVATION_REQUESTS)
                    .where(
                        ACTIVATION_REQUESTS.c.status
                        == ActivationStatus.PENDING.value
                    )
                )
                or 0
            )
            if pending >= MAX_PENDING_ACTIVATIONS:
                raise ActivationCapacityError

            subject_digest = activation_subject_digest(command)
            existing = (
                (
                    await connection.execute(
                        sa.select(ACTIVATION_REQUESTS).where(
                            ACTIVATION_REQUESTS.c.tenant_id == command.tenant_id,
                            ACTIVATION_REQUESTS.c.environment_id
                            == command.environment_id,
                            ACTIVATION_REQUESTS.c.provider == command.provider.value,
                            ACTIVATION_REQUESTS.c.subject_ref_digest
                            == subject_digest,
                            ACTIVATION_REQUESTS.c.status
                            == ActivationStatus.PENDING.value,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return row_to_activation_request(existing)

            request = ActivationRequest(
                request_id=str(uuid.uuid4()),
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                provider=command.provider,
                subject_ref=command.subject_ref,
                subject_ref_digest=subject_digest,
                source=command.source,
                source_event_digest=(
                    None
                    if command.source_event_ref is None
                    else activation_source_ref_digest(
                        kind="event", reference=command.source_event_ref
                    )
                ),
                source_chat_digest=(
                    None
                    if command.source_chat_ref is None
                    else activation_source_ref_digest(
                        kind="chat", reference=command.source_chat_ref
                    )
                ),
                requested_at=now,
                expires_at=now
                + _dt.timedelta(seconds=ACTIVATION_TTL_SECONDS),
                status=ActivationStatus.PENDING,
            )
            await connection.execute(
                sa.insert(ACTIVATION_REQUESTS).values(
                    **activation_request_to_row(request)
                )
            )
            return request

    @_persistence_boundary(write=False)
    async def load(self, *, query: ActivationLookup) -> ActivationRequest | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(ACTIVATION_REQUESTS).where(
                            ACTIVATION_REQUESTS.c.request_id == query.request_id,
                            ACTIVATION_REQUESTS.c.tenant_id == query.tenant_id,
                            ACTIVATION_REQUESTS.c.environment_id
                            == query.environment_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else row_to_activation_request(row)


async def _insert_audit_event(
    connection: AsyncConnection, candidate: AdminAuditCandidate, *, now: _dt.datetime
) -> AdminAuditEvent | None:
    """``admin_audit_events`` 的**唯一**落库口；同阶段事实已存在时返回 ``None``。

    目录路径与 ``PostgresAdminAuditStore._write`` 都经过这里。两条路径各写一份
    INSERT，就是两份列映射，而两份列映射迟早分叉——分叉之后审计表里会同时存在
    两种形状的行，且没有任何东西会报错。

    ``ON CONFLICT DO NOTHING`` **不带推断目标**：一条 INSERT 可能撞上主键，也可能
    撞上两条偏唯一索引中的一条，带目标时只推断一条。它吞掉的只有唯一/主键/偏唯一
    三种冲突；CHECK、外键、NOT NULL 一律照抛，由 ``_write_transaction`` 收敛。

    返回 ``None`` 而不是自己抛：同一个事实经两个公开入口暴露时是两个错误族，由
    调用者按自己的入口决定抛哪个。
    """
    event = seal(candidate, now=now)
    inserted = await connection.scalar(
        sa.dialects.postgresql.insert(ADMIN_AUDIT_EVENTS)
        .values(**admin_audit_event_to_row(event))
        .on_conflict_do_nothing()
        .returning(ADMIN_AUDIT_EVENTS.c.event_id)
    )
    return None if inserted is None else event


class PostgresAdminAuditStore:
    """``AdminAuditStore`` 的 PostgreSQL 实现；三个窄写方法 + 一个按 id 读。

    ``persistence`` 里针对 ``admin_audit_events`` 的 ``sa.update`` / ``sa.delete``
    一律不存在，由 ``tests/security/test_admin_audit_append_only.py`` 钉住。
    """

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def _append_candidate(
        self, candidate: AdminAuditCandidate
    ) -> AdminAuditEvent:
        """``append_started`` 与 ``append_denied`` 共用的落库包装。

        名字取得具体而不是叫 ``_write``：调用点冻结按函数名匹配，一个泛用名会把
        将来任何一个无关的 ``_write`` 都拖进违规名单，而噪音会让名单没人看。
        """
        async with _write_transaction(self._engine) as connection:
            event = await _insert_audit_event(
                connection, candidate, now=self._clock()
            )
            if event is None:
                raise AdminAuditConflictError
            return event
        raise AdminAuditConflictError  # pragma: no cover - 事务体必然 return 或抛

    @_persistence_boundary(write=True)
    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent:
        return await self._append_candidate(
            AdminAuditCandidate(
                operation_id=start.operation_id,
                tenant_id=start.tenant_id,
                environment_id=start.environment_id,
                actor_user_id=start.actor_user_id,
                actor=start.actor,
                auth_source=start.auth_source,
                action=start.action,
                target_kind=start.target_kind,
                target_ref_digest=start.target_ref_digest,
                outcome=AdminAuditOutcome.STARTED,
            )
        )

    @_persistence_boundary(write=True)
    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent:
        return await self._append_candidate(
            AdminAuditCandidate(
                operation_id=denial.operation_id,
                tenant_id=denial.tenant_id,
                environment_id=denial.environment_id,
                actor_user_id=denial.actor_user_id,
                actor=denial.actor,
                auth_source=denial.auth_source,
                action=denial.action,
                target_kind=denial.target_kind,
                target_ref_digest=denial.target_ref_digest,
                outcome=AdminAuditOutcome.DENIED,
                reason_code=denial.reason_code,
            )
        )

    @_persistence_boundary(write=True)
    async def append_terminal(
        self, *, terminal: AdminAuditTerminal
    ) -> AdminAuditEvent:
        async with _write_transaction(self._engine) as connection:
            started = (
                (
                    await connection.execute(
                        sa.select(ADMIN_AUDIT_EVENTS).where(
                            ADMIN_AUDIT_EVENTS.c.operation_id == terminal.operation_id,
                            ADMIN_AUDIT_EVENTS.c.outcome
                            == AdminAuditOutcome.STARTED.value,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if started is None:
                raise AdminAuditMissingStartError
            origin = row_to_admin_audit_event(started)
            # 稳定字段从已存的 STARTED 读回，不由调用方再传一遍。
            event = await _insert_audit_event(
                connection,
                AdminAuditCandidate(
                    operation_id=origin.operation_id,
                    tenant_id=origin.tenant_id,
                    environment_id=origin.environment_id,
                    actor_user_id=origin.actor_user_id,
                    actor=origin.actor,
                    auth_source=origin.auth_source,
                    action=origin.action,
                    target_kind=origin.target_kind,
                    target_ref_digest=origin.target_ref_digest,
                    outcome=terminal.outcome,
                    reason_code=terminal.reason_code,
                    effect=terminal.effect,
                ),
                now=self._clock(),
            )
            if event is None:
                raise AdminAuditConflictError
            return event
        raise AdminAuditConflictError  # pragma: no cover - 同上

    @_persistence_boundary(write=False)
    async def load(self, *, event_id: str) -> AdminAuditEvent | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(ADMIN_AUDIT_EVENTS).where(
                            ADMIN_AUDIT_EVENTS.c.event_id == event_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else row_to_admin_audit_event(row)


class PostgresUserDirectoryStore:
    """``UserDirectoryStore`` 的 PostgreSQL 实现。

    授权改变与它的审计在**一个** ``_write_transaction`` 里提交：审计写不进去，
    整个授权改变回滚，一行不留。已知冲突用 ``ON CONFLICT DO NOTHING ... RETURNING``
    在事务体内探测，不捕获 ``IntegrityError``、不读约束名——那是仓库既有做法，也是
    唯一能把"未知约束"与"业务冲突"分开的做法。
    """

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    @_persistence_boundary(write=False)
    async def load_account(
        self, *, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None:
        async with self._engine.connect() as connection:
            return await self._facts(
                connection,
                user_id=user_id,
                tenant_id=tenant_id,
                environment_id=environment_id,
            )

    @_persistence_boundary(write=False)
    async def resolve_by_subject(
        self,
        *,
        provider: IdentitySource,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
    ) -> DirectoryPrincipalFacts | None:
        digest = external_subject_digest(
            provider=provider,
            tenant_id=tenant_id,
            environment_id=environment_id,
            subject_ref=subject_ref,
        )
        async with self._engine.connect() as connection:
            user_id = await connection.scalar(
                sa.select(EXTERNAL_IDENTITIES.c.user_id).where(
                    EXTERNAL_IDENTITIES.c.provider == provider.value,
                    EXTERNAL_IDENTITIES.c.tenant_id == tenant_id,
                    EXTERNAL_IDENTITIES.c.environment_id == environment_id,
                    EXTERNAL_IDENTITIES.c.subject_ref_digest == digest,
                )
            )
            if user_id is None:
                return None
            facts = await self._facts(
                connection,
                user_id=user_id,
                tenant_id=tenant_id,
                environment_id=environment_id,
            )
            if facts is None:
                raise UserDirectorySubjectUnavailableError(
                    "the bound subject is unavailable"
                )
            return facts

    @_persistence_boundary(write=True)
    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        async with _write_transaction(self._engine) as connection:
            return await self._apply_in_transaction(
                connection, command=command, context=context
            )
        return ()  # pragma: no cover - 事务体必然 return 或抛

    # ---- 读 ----------------------------------------------------------------

    async def _facts(
        self,
        connection: AsyncConnection,
        *,
        user_id: str,
        tenant_id: str,
        environment_id: str,
    ) -> DirectoryPrincipalFacts | None:
        """停用的账号解析不出任何授权事实——停用即刻生效。"""
        account_row = (
            (
                await connection.execute(
                    sa.select(USER_ACCOUNTS).where(
                        USER_ACCOUNTS.c.user_id == user_id,
                        USER_ACCOUNTS.c.status == UserStatus.ACTIVE.value,
                    )
                )
            )
            .mappings()
            .first()
        )
        if account_row is None:
            return None
        assignment_row = (
            (
                await connection.execute(
                    sa.select(USER_ROLE_ASSIGNMENTS).where(
                        USER_ROLE_ASSIGNMENTS.c.user_id == user_id,
                        USER_ROLE_ASSIGNMENTS.c.tenant_id == tenant_id,
                        USER_ROLE_ASSIGNMENTS.c.environment_id == environment_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if assignment_row is None:
            return None
        return DirectoryPrincipalFacts(
            account=UserAccount(
                user_id=account_row["user_id"],
                actor=account_row["actor"],
                display_name=account_row["display_name"],
                status=UserStatus(account_row["status"]),
                created_at=account_row["created_at"],
                updated_at=account_row["updated_at"],
            ),
            assignment=UserRoleAssignment(
                user_id=assignment_row["user_id"],
                tenant_id=assignment_row["tenant_id"],
                environment_id=assignment_row["environment_id"],
                role=ProductRole(assignment_row["role"]),
                created_by=assignment_row["created_by"],
                created_at=assignment_row["created_at"],
                updated_at=assignment_row["updated_at"],
            ),
        )

    async def _account_exists(
        self, connection: AsyncConnection, user_id: str
    ) -> bool:
        return (
            await connection.scalar(
                sa.select(USER_ACCOUNTS.c.user_id).where(
                    USER_ACCOUNTS.c.user_id == user_id
                )
            )
        ) is not None

    async def _role_exists(
        self,
        connection: AsyncConnection,
        *,
        user_id: str,
        tenant_id: str,
        environment_id: str,
    ) -> bool:
        return (
            await connection.scalar(
                sa.select(USER_ROLE_ASSIGNMENTS.c.role).where(
                    USER_ROLE_ASSIGNMENTS.c.user_id == user_id,
                    USER_ROLE_ASSIGNMENTS.c.tenant_id == tenant_id,
                    USER_ROLE_ASSIGNMENTS.c.environment_id == environment_id,
                )
            )
        ) is not None

    # ---- 写：本方法与 ``_bootstrap_in_transaction`` 是四类授权事实的全部写入口 ----

    async def _apply_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        command: DirectoryCommand,
        context: AdminOperationContext,
    ) -> tuple[AdminAuditEvent, ...]:
        now = self._clock()
        if isinstance(command, (ApproveActivationCommand, RejectActivationCommand)):
            return await self._decide_activation(
                connection, command=command, context=context, now=now
            )
        if isinstance(command, BootstrapLocalAdminCommand):
            return await self._bootstrap_in_transaction(
                connection, command=command, context=context, now=now
            )
        if isinstance(command, MigrateLegacyIdentitiesCommand):
            events: list[AdminAuditEvent] = []
            for index, entry in enumerate(command.entries, start=1):
                await self._insert_account(
                    connection,
                    user_id=entry.user_id,
                    actor=entry.actor,
                    display_name=entry.display_name,
                    now=now,
                )
                await self._upsert_role(
                    connection,
                    user_id=entry.user_id,
                    tenant_id=command.tenant_id,
                    environment_id=command.environment_id,
                    role=entry.role,
                    created_by=context.actor_user_id,
                    now=now,
                )
                await self._bind_subject(
                    connection,
                    user_id=entry.user_id,
                    tenant_id=command.tenant_id,
                    environment_id=command.environment_id,
                    subject_ref=entry.subject_ref,
                    now=now,
                )
                events.append(
                    await self._audit(
                        connection,
                        derive_audit(
                            command,
                            context,
                            target_ref=entry.user_id,
                            operation_id=batch_operation_id(context, index),
                            entry=entry,
                        ),
                        now=now,
                    )
                )
            return tuple(events)
        if isinstance(command, CreateUserCommand):
            await self._insert_account(
                connection,
                user_id=command.user_id,
                actor=command.actor,
                display_name=command.display_name,
                now=now,
            )
            await self._upsert_role(
                connection,
                user_id=command.user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                role=command.role,
                created_by=context.actor_user_id,
                now=now,
            )
        elif isinstance(command, SetUserStatusCommand):
            if not await self._account_exists(connection, command.user_id):
                raise UserDirectoryNotFoundError("no such account")
            # 作用域里没有角色的账号在这个作用域根本不可见，改它的状态等于隔着
            # 作用域动一个看不到的人。
            if not await self._role_exists(
                connection,
                user_id=command.user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
            ):
                raise UserDirectoryNotFoundError("no role in this scope")
            await connection.execute(
                sa.update(USER_ACCOUNTS)
                .where(USER_ACCOUNTS.c.user_id == command.user_id)
                .values(status=command.status.value, updated_at=now)
            )
        elif isinstance(command, AssignRoleCommand):
            if not await self._account_exists(connection, command.user_id):
                raise UserDirectoryNotFoundError("no such account")
            await self._upsert_role(
                connection,
                user_id=command.user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                role=command.role,
                created_by=context.actor_user_id,
                now=now,
            )
        elif isinstance(command, RevokeRoleCommand):
            if not await self._account_exists(connection, command.user_id):
                raise UserDirectoryNotFoundError("no such account")
            deleted = await connection.scalar(
                sa.delete(USER_ROLE_ASSIGNMENTS)
                .where(
                    USER_ROLE_ASSIGNMENTS.c.user_id == command.user_id,
                    USER_ROLE_ASSIGNMENTS.c.tenant_id == command.tenant_id,
                    USER_ROLE_ASSIGNMENTS.c.environment_id == command.environment_id,
                )
                .returning(USER_ROLE_ASSIGNMENTS.c.user_id)
            )
            if deleted is None:
                raise UserDirectoryNotFoundError("no role in this scope")
        elif isinstance(command, BindExternalIdentityCommand):
            if not await self._account_exists(connection, command.user_id):
                raise UserDirectoryNotFoundError("no such account")
            await self._bind_subject(
                connection,
                user_id=command.user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                subject_ref=command.subject_ref,
                now=now,
            )
        else:
            if not await self._account_exists(connection, command.user_id):
                raise UserDirectoryNotFoundError("no such account")
            # 前置判定**就是**这条写语句，不是它前面的一次 SELECT。
            #
            # 先 SELECT 出摘要、再发一条不看结果的 DELETE，中间就有一个窗口：另一个
            # 事务在这两步之间解绑并提交之后，我们的 DELETE 影响 0 行，而下面那条
            # 审计照样记 SUCCEEDED——一次没有发生的授权改变，在审计里留下了发生过的
            # 证据。这与 ``RevokeRoleCommand`` 是同一条写法，不是两套。
            removed = await connection.scalar(
                sa.delete(EXTERNAL_IDENTITIES)
                .where(
                    EXTERNAL_IDENTITIES.c.provider == IdentitySource.FEISHU.value,
                    EXTERNAL_IDENTITIES.c.tenant_id == command.tenant_id,
                    EXTERNAL_IDENTITIES.c.environment_id == command.environment_id,
                    EXTERNAL_IDENTITIES.c.user_id == command.user_id,
                )
                .returning(EXTERNAL_IDENTITIES.c.subject_ref_digest)
            )
            if removed is None:
                raise UserDirectoryNotFoundError("no binding in this scope")
        return (
            await self._audit(
                connection,
                derive_audit(command, context, target_ref=command.user_id),
                now=now,
            ),
        )

    async def _require_activation_admin_for_update(
        self,
        connection: AsyncConnection,
        *,
        tenant_id: str,
        environment_id: str,
        context: AdminOperationContext,
    ) -> None:
        if context.auth_source is not IdentitySource.LOCAL_ADMIN:
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED
            )
        account = (
            (
                await connection.execute(
                    sa.select(USER_ACCOUNTS)
                    .where(USER_ACCOUNTS.c.user_id == context.actor_user_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        assignment = (
            (
                await connection.execute(
                    sa.select(USER_ROLE_ASSIGNMENTS)
                    .where(
                        USER_ROLE_ASSIGNMENTS.c.user_id == context.actor_user_id,
                        USER_ROLE_ASSIGNMENTS.c.tenant_id == tenant_id,
                        USER_ROLE_ASSIGNMENTS.c.environment_id == environment_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            account is None
            or account["status"] != UserStatus.ACTIVE.value
            or account["actor"] != context.actor
            or assignment is None
            or assignment["role"] != ProductRole.ADMIN.value
        ):
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.ACTOR_NOT_ADMIN
            )

    async def _activation_for_update(
        self,
        connection: AsyncConnection,
        *,
        request_id: str,
        tenant_id: str,
        environment_id: str,
    ) -> ActivationRequest:
        row = (
            (
                await connection.execute(
                    sa.select(ACTIVATION_REQUESTS)
                    .where(ACTIVATION_REQUESTS.c.request_id == request_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.TARGET_NOT_FOUND
            )
        request = row_to_activation_request(row)
        if (
            request.tenant_id != tenant_id
            or request.environment_id != environment_id
        ):
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.SCOPE_MISMATCH
            )
        return request

    async def _decide_activation(
        self,
        connection: AsyncConnection,
        *,
        command: ApproveActivationCommand | RejectActivationCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        await self._require_activation_admin_for_update(
            connection,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            context=context,
        )
        request = await self._activation_for_update(
            connection,
            request_id=command.request_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
        )
        if request.status is not ActivationStatus.PENDING:
            raise UserDirectoryConflictError("the activation request is terminal")
        if request.expires_at <= now:
            raise UserDirectoryConflictError("the activation request expired")

        if isinstance(command, ApproveActivationCommand):
            await self._approve_activation_facts(
                connection,
                command=command,
                request=request,
                context=context,
                now=now,
            )
            values = {
                "status": ActivationStatus.APPROVED.value,
                "decided_at": now,
                "decided_by": context.actor_user_id,
                "approved_role": command.approved_role.value,
            }
        else:
            values = {
                "status": ActivationStatus.REJECTED.value,
                "decided_at": now,
                "decided_by": context.actor_user_id,
                "approved_role": None,
            }
        changed = await connection.scalar(
            sa.update(ACTIVATION_REQUESTS)
            .where(
                ACTIVATION_REQUESTS.c.request_id == request.request_id,
                ACTIVATION_REQUESTS.c.tenant_id == command.tenant_id,
                ACTIVATION_REQUESTS.c.environment_id == command.environment_id,
                ACTIVATION_REQUESTS.c.status == ActivationStatus.PENDING.value,
                ACTIVATION_REQUESTS.c.expires_at > now,
            )
            .values(**values)
            .returning(ACTIVATION_REQUESTS.c.request_id)
        )
        if changed is None:
            raise UserDirectoryConflictError("the activation request changed")
        return (
            await self._audit(
                connection,
                derive_audit(command, context, target_ref=request.request_id),
                now=now,
            ),
        )

    async def _approve_activation_facts(
        self,
        connection: AsyncConnection,
        *,
        command: ApproveActivationCommand,
        request: ActivationRequest,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> None:
        account = (
            (
                await connection.execute(
                    sa.select(USER_ACCOUNTS)
                    .where(USER_ACCOUNTS.c.actor == command.actor)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if account is not None:
            if (
                account["status"] != UserStatus.ACTIVE.value
                or account["display_name"] != command.display_name
            ):
                raise UserDirectoryConflictError(
                    "the activation account conflicts"
                )
            user_id = str(account["user_id"])
        else:
            user_id = activation_user_id(
                subject_ref_digest=request.subject_ref_digest
            )
            account = (
                (
                    await connection.execute(
                        sa.select(USER_ACCOUNTS)
                        .where(USER_ACCOUNTS.c.user_id == user_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
        if account is None:
            await self._insert_account(
                connection,
                user_id=user_id,
                actor=command.actor,
                display_name=command.display_name,
                now=now,
            )
        elif (
            account["actor"] != command.actor
            or account["display_name"] != command.display_name
            or account["status"] != UserStatus.ACTIVE.value
        ):
            raise UserDirectoryConflictError("the activation account conflicts")

        assignment = (
            (
                await connection.execute(
                    sa.select(USER_ROLE_ASSIGNMENTS)
                    .where(
                        USER_ROLE_ASSIGNMENTS.c.user_id == user_id,
                        USER_ROLE_ASSIGNMENTS.c.tenant_id == command.tenant_id,
                        USER_ROLE_ASSIGNMENTS.c.environment_id
                        == command.environment_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if assignment is None:
            await self._upsert_role(
                connection,
                user_id=user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                role=command.approved_role,
                created_by=context.actor_user_id,
                now=now,
            )
        elif assignment["role"] != command.approved_role.value:
            raise UserDirectoryConflictError("the existing role differs")

        digest = external_subject_digest(
            provider=IdentitySource.FEISHU,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            subject_ref=request.subject_ref,
        )
        subject_owner = await connection.scalar(
            sa.select(EXTERNAL_IDENTITIES.c.user_id)
            .where(
                EXTERNAL_IDENTITIES.c.provider == IdentitySource.FEISHU.value,
                EXTERNAL_IDENTITIES.c.tenant_id == command.tenant_id,
                EXTERNAL_IDENTITIES.c.environment_id == command.environment_id,
                EXTERNAL_IDENTITIES.c.subject_ref_digest == digest,
            )
            .with_for_update()
        )
        if subject_owner is None:
            await self._bind_subject(
                connection,
                user_id=user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                subject_ref=request.subject_ref,
                now=now,
            )
        elif subject_owner != user_id:
            raise UserDirectoryConflictError("the activation subject conflicts")

    async def _bootstrap_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        command: BootstrapLocalAdminCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        """四态表的四行都在这里；判定读的是**目录链接是否完整**，不是凭据行在不在。

        按"已有凭据行即 no-op"实现，任何跑过 RI5 的库升级之后 ``user_id`` 永远是
        ``NULL``、``user_accounts`` 不会出现、ADMIN 角色不会出现——而全新安装的
        测试全绿，因为全新安装走的是第一行。
        """
        await connection.execute(_BOOTSTRAP_LOCAL_ADMIN_LOCK)
        credential = (
            (
                await connection.execute(
                    sa.select(LOCAL_ADMINS.c.id, LOCAL_ADMINS.c.user_id)
                    .where(LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if credential is not None and credential["user_id"] is not None:
            linked = credential["user_id"]
            role = await connection.scalar(
                sa.select(USER_ROLE_ASSIGNMENTS.c.role).where(
                    USER_ROLE_ASSIGNMENTS.c.user_id == linked,
                    USER_ROLE_ASSIGNMENTS.c.tenant_id == command.tenant_id,
                    USER_ROLE_ASSIGNMENTS.c.environment_id == command.environment_id,
                )
            )
            if not await self._account_exists(connection, linked):
                raise UserDirectoryConflictError("the credential link is broken")
            if role != ProductRole.ADMIN.value:
                raise UserDirectoryConflictError("the credential link is broken")
            return ()
        await self._insert_account(
            connection,
            user_id=command.user_id,
            actor=command.actor,
            display_name=command.display_name,
            now=now,
        )
        await self._upsert_role(
            connection,
            user_id=command.user_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            role=ProductRole.ADMIN,
            created_by=context.actor_user_id,
            now=now,
        )
        if credential is None:
            await connection.execute(
                sa.insert(LOCAL_ADMINS).values(
                    id=LOCAL_ADMIN_SINGLETON_ID,
                    password_hash=command.password_hash,
                    must_change_password=True,
                    updated_at=now,
                    user_id=command.user_id,
                )
            )
        else:
            # backfill **保留原 password_hash 与原 must_change_password**：管理员
            # 可能早就改过密码，拿传进来的初始口令覆盖回去等于一次静默的凭据回滚。
            await connection.execute(
                sa.update(LOCAL_ADMINS)
                .where(LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID)
                .values(user_id=command.user_id, updated_at=now)
            )
        return (
            await self._audit(
                connection,
                derive_audit(command, context, target_ref=command.user_id),
                now=now,
            ),
        )

    async def _audit(
        self,
        connection: AsyncConnection,
        candidate: AdminAuditCandidate,
        *,
        now: _dt.datetime,
    ) -> AdminAuditEvent:
        event = await _insert_audit_event(connection, candidate, now=now)
        if event is None:
            raise AdminAuditUnwritableError
        return event

    async def _insert_account(
        self,
        connection: AsyncConnection,
        *,
        user_id: str,
        actor: str,
        display_name: str,
        now: _dt.datetime,
    ) -> None:
        inserted = await connection.scalar(
            sa.dialects.postgresql.insert(USER_ACCOUNTS)
            .values(
                user_id=user_id,
                actor=actor,
                display_name=display_name,
                status=UserStatus.ACTIVE.value,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing()
            .returning(USER_ACCOUNTS.c.user_id)
        )
        if inserted is None:
            raise UserDirectoryConflictError("the account or actor is already taken")

    async def _upsert_role(
        self,
        connection: AsyncConnection,
        *,
        user_id: str,
        tenant_id: str,
        environment_id: str,
        role: ProductRole,
        created_by: str,
        now: _dt.datetime,
    ) -> None:
        statement = sa.dialects.postgresql.insert(USER_ROLE_ASSIGNMENTS).values(
            user_id=user_id,
            tenant_id=tenant_id,
            environment_id=environment_id,
            role=role.value,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id", "tenant_id", "environment_id"],
                set_={"role": role.value, "updated_at": now},
            )
        )

    async def _bind_subject(
        self,
        connection: AsyncConnection,
        *,
        user_id: str,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
        now: _dt.datetime,
    ) -> None:
        inserted = await connection.scalar(
            sa.dialects.postgresql.insert(EXTERNAL_IDENTITIES)
            .values(
                provider=IdentitySource.FEISHU.value,
                tenant_id=tenant_id,
                environment_id=environment_id,
                subject_ref_digest=external_subject_digest(
                    provider=IdentitySource.FEISHU,
                    tenant_id=tenant_id,
                    environment_id=environment_id,
                    subject_ref=subject_ref,
                ),
                user_id=user_id,
                created_at=now,
                last_seen_at=now,
            )
            .on_conflict_do_nothing()
            .returning(EXTERNAL_IDENTITIES.c.user_id)
        )
        if inserted is None:
            raise UserDirectoryConflictError("the subject or the account is bound")

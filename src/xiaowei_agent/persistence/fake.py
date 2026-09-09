"""单进程 TaskStore 与 ChannelStore 实现。

**真实执行**：幂等作用域、expected-version 检查、返回存储层 winner、lease/fencing
闭合规则、终态保护、迁移表闭集。
**不宣称**：跨进程原子性、崩溃恢复、PostgreSQL 级隔离——这些属于 M4。

用一把 ``asyncio.Lock`` 串行化全部写入，使单进程内的 CAS 语义确定；这不等于分布式
原子性，测试与文档都不这样表述。

**判定规则不在本模块**。拒绝顺序、fencing 闭合真值表、租约与续租条件、迁移后的记录
形状，全部由 ``persistence/decisions.py`` 的纯函数持有，M4 的 PostgreSQL 实现共用同
一份。本模块只负责"怎么存"：一把锁、两个字典、一个自增 token。

这样分工是为了让两个实现不可能各自跑偏——判定只有一份，分叉无处发生。**不要在本
模块里就地重写任何判定**，哪怕只是"顺手内联一个条件"：那正是分叉的起点。
渠道绑定和投影状态同样只复用 ``persistence/channel.py`` 的纯判定。
"""

import datetime as _dt
import uuid
from typing import Final, TypeVar

from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ActorTaskPageQuery,
    ApprovalRequest,
    ChannelKind,
    GrantRejection,
    LeaseGrant,
    ProjectionState,
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
)
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    ChannelBinding,
    ChannelBindingConflictError,
    ChannelBindingNotFoundError,
    ClaimedTaskLookup,
    ClaimProjectionCommand,
    CompleteProjectionCommand,
    CreateProjectionSubscriptionCommand,
    DeadLetterProjectionCommand,
    GroupBindingLookup,
    GroupBoundTaskIdsQuery,
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
from xiaowei_agent.persistence.decisions import (
    apply_transition,
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
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import (
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
    request_dedup_digest,
    retry_command_digest,
    step_commit_digest,
    submission_digest,
    submission_matches_record,
    validate_task_failure_limit,
)
from xiaowei_agent.persistence.web_session import (
    ConsumeOAuthStateCommand,
    IssueOAuthStateCommand,
    OAuthState,
    OAuthStateNotFoundError,
    RevokeWebSessionCommand,
    RotateWebSessionCommand,
    WebSession,
    WebSessionConflictError,
    WebSessionLookup,
    WebSessionNotFoundError,
)

_EntryT = TypeVar("_EntryT")

IS_FAKE: Final[bool] = True
"""供 tests/security/test_fake_isolation.py 断言；生产模块不得导入本模块。"""


class InMemoryChannelStore:
    """与 TaskStore 共锁的单进程渠道存储；绑定和订阅可以原子恢复。"""

    def __init__(self, *, clock: Clock, state: InMemoryPersistenceState | None = None) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    def _require_task(self, task_id: str) -> TaskRecord:
        try:
            return self._state.tasks[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id=task_id) from exc

    def _require_subscription(self, subscription_id: str) -> ProjectionSubscription:
        try:
            return self._state.projection_subscriptions[subscription_id]
        except KeyError as exc:
            raise ProjectionSubscriptionNotFoundError from exc

    def _create_subscription_locked(
        self, command: CreateProjectionSubscriptionCommand
    ) -> ProjectionSubscription:
        self._require_task(command.task_id)
        key = (
            command.task_id,
            command.destination_kind.value,
            command.destination_ref,
        )
        existing_id = self._state.projection_subscription_ids_by_destination.get(key)
        if existing_id is not None:
            existing = self._state.projection_subscriptions[existing_id]
            if not subscription_matches_command(existing, command):
                raise ProjectionSubscriptionConflictError
            return existing
        subscription = ProjectionSubscription(
            subscription_id=str(uuid.uuid4()),
            task_id=command.task_id,
            destination_kind=command.destination_kind,
            destination_ref=command.destination_ref,
            state=command.initial_state,
            attempt_number=0,
            next_attempt_at=command.next_attempt_at,
            provider_failure_count=0,
        )
        self._state.projection_subscriptions[subscription.subscription_id] = subscription
        self._state.projection_subscription_ids_by_destination[key] = (
            subscription.subscription_id
        )
        return subscription

    async def bind_task(self, *, command: BindTaskCommand) -> ChannelBinding:
        async with self._lock:
            task = self._require_task(command.task_id)
            if (
                task.tenant_id != command.tenant_id
                or task.environment_id != command.environment_id
            ):
                raise TaskNotFoundError(task_id=command.task_id)
            source_key = (
                command.tenant_id,
                command.environment_id,
                command.channel.value,
                command.source_event_ref,
            )
            source_id = self._state.channel_binding_ids_by_source.get(source_key)
            task_binding_id = self._state.channel_binding_ids_by_task.get(command.task_id)
            existing_id = source_id if source_id is not None else task_binding_id
            if existing_id is not None:
                existing = self._state.channel_bindings[existing_id]
                if (
                    source_id != task_binding_id
                    or not binding_matches_command(existing, command)
                ):
                    raise ChannelBindingConflictError
                if command.projection is not None:
                    self._create_subscription_locked(command.projection)
                return existing

            binding = ChannelBinding(
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
            if command.projection is not None:
                self._create_subscription_locked(command.projection)
            self._state.channel_bindings[binding.binding_id] = binding
            self._state.channel_binding_ids_by_source[source_key] = binding.binding_id
            self._state.channel_binding_ids_by_task[command.task_id] = binding.binding_id
            return binding

    async def get_group_binding(self, *, lookup: GroupBindingLookup) -> ChannelBinding:
        async with self._lock:
            binding_id = self._state.channel_binding_ids_by_task.get(lookup.task_id)
            if binding_id is None:
                raise ChannelBindingNotFoundError
            binding = self._state.channel_bindings[binding_id]
            if (
                binding.tenant_id != lookup.tenant_id
                or binding.environment_id != lookup.environment_id
                or binding.channel is not ChannelKind.FEISHU_GROUP
            ):
                raise ChannelBindingNotFoundError
            return binding

    async def list_group_bound_task_ids(
        self, *, query: GroupBoundTaskIdsQuery
    ) -> frozenset[str]:
        async with self._lock:
            task_ids: set[str] = set()
            for task_id in query.task_ids:
                binding_id = self._state.channel_binding_ids_by_task.get(task_id)
                if binding_id is None:
                    continue
                binding = self._state.channel_bindings[binding_id]
                if (
                    binding.tenant_id == query.tenant_id
                    and binding.environment_id == query.environment_id
                    and binding.channel is ChannelKind.FEISHU_GROUP
                ):
                    task_ids.add(task_id)
            return frozenset(task_ids)

    async def create_projection_subscription(
        self, *, command: CreateProjectionSubscriptionCommand
    ) -> ProjectionSubscription:
        async with self._lock:
            return self._create_subscription_locked(command)

    async def list_due_projection_subscriptions(
        self, *, query: ProjectionDueQuery
    ) -> tuple[ProjectionSubscription, ...]:
        now = self._clock()
        async with self._lock:
            due = []
            for subscription in self._state.projection_subscriptions.values():
                binding_id = self._state.channel_binding_ids_by_task.get(
                    subscription.task_id
                )
                binding = (
                    None
                    if binding_id is None
                    else self._state.channel_bindings[binding_id]
                )
                if (
                    binding is None
                    or binding.tenant_id != query.tenant_id
                    or binding.environment_id != query.environment_id
                    or subscription.state
                    in {ProjectionState.COMPLETED, ProjectionState.DEAD_LETTER}
                    or subscription.next_attempt_at > now
                    or (
                        subscription.claim_expires_at is not None
                        and subscription.claim_expires_at > now
                    )
                ):
                    continue
                due.append(subscription)
            return tuple(
                sorted(
                    due,
                    key=lambda item: (item.next_attempt_at, item.subscription_id),
                )[: query.limit]
            )

    def _save(self, result: ProjectionUpdateResult) -> ProjectionUpdateResult:
        if result.applied:
            self._state.projection_subscriptions[
                result.winner.subscription_id
            ] = result.winner
        return result

    async def claim_projection_subscription(
        self, *, command: ClaimProjectionCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            current = self._require_subscription(command.subscription_id)
            result = claim_projection(
                current,
                command,
                now=self._clock(),
                fencing_token=self._state.next_projection_fencing_token,
            )
            if result.applied:
                self._state.next_projection_fencing_token += 1
            return self._save(result)

    async def resolve_claimed_task_lookup(
        self, *, lookup: ClaimedTaskLookup
    ) -> TaskLookup:
        async with self._lock:
            subscription = self._state.projection_subscriptions.get(
                lookup.subscription_id
            )
            binding = None
            if subscription is not None:
                binding_id = self._state.channel_binding_ids_by_task.get(
                    subscription.task_id
                )
                if binding_id is not None:
                    binding = self._state.channel_bindings[binding_id]
            if subscription is None:
                raise ProjectionClaimNotFoundError
            return authorize_claimed_task_lookup(
                subscription,
                binding,
                lookup,
                now=self._clock(),
            )

    async def renew_projection_claim(
        self, *, command: RenewProjectionClaimCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            return self._save(
                renew_projection_claim(
                    self._require_subscription(command.subscription_id),
                    command,
                    now=self._clock(),
                )
            )

    async def record_initial_projection(
        self, *, command: RecordInitialProjectionCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            return self._save(
                record_initial_projection(
                    self._require_subscription(command.subscription_id),
                    command,
                    now=self._clock(),
                )
            )

    async def schedule_task_recheck(
        self, *, command: ScheduleTaskRecheckCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            return self._save(
                schedule_task_recheck(
                    self._require_subscription(command.subscription_id),
                    command,
                    now=self._clock(),
                )
            )

    async def schedule_provider_retry(
        self, *, command: ScheduleProviderRetryCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            return self._save(
                schedule_provider_retry(
                    self._require_subscription(command.subscription_id),
                    command,
                    now=self._clock(),
                )
            )

    async def complete_projection(
        self, *, command: CompleteProjectionCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            return self._save(
                complete_projection(
                    self._require_subscription(command.subscription_id),
                    command,
                    now=self._clock(),
                )
            )

    async def dead_letter_projection(
        self, *, command: DeadLetterProjectionCommand
    ) -> ProjectionUpdateResult:
        async with self._lock:
            return self._save(
                dead_letter_projection(
                    self._require_subscription(command.subscription_id),
                    command,
                    now=self._clock(),
                )
            )


class InMemoryWebSessionStore:
    """与其他内存存储共锁的 OAuth state 和浏览器 session 实现。"""

    def __init__(self, *, clock: Clock, state: InMemoryPersistenceState | None = None) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    async def issue_oauth_state(
        self, *, command: IssueOAuthStateCommand
    ) -> OAuthState:
        async with self._lock:
            if command.state_digest in self._state.oauth_states:
                raise WebSessionConflictError
            now = self._clock()
            state = OAuthState(
                state_digest=command.state_digest,
                issued_at=now,
                expires_at=now + _dt.timedelta(seconds=command.ttl_seconds),
            )
            self._state.oauth_states[state.state_digest] = state
            return state

    async def consume_oauth_state(
        self, *, command: ConsumeOAuthStateCommand
    ) -> OAuthState:
        async with self._lock:
            state = self._state.oauth_states.get(command.state_digest)
            now = self._clock()
            if (
                state is None
                or state.consumed_at is not None
                or now >= state.expires_at
            ):
                raise OAuthStateNotFoundError
            consumed = state.model_copy(update={"consumed_at": now})
            self._state.oauth_states[state.state_digest] = consumed
            return consumed

    async def rotate_session(
        self, *, command: RotateWebSessionCommand
    ) -> WebSession:
        async with self._lock:
            if command.session_digest in self._state.web_sessions:
                raise WebSessionConflictError
            now = self._clock()
            candidate = WebSession(
                session_digest=command.session_digest,
                subject_ref=command.subject_ref,
                issued_at=now,
                expires_at=now + _dt.timedelta(seconds=command.ttl_seconds),
            )
            previous_digest = command.previous_session_digest
            if previous_digest is not None:
                previous = self._state.web_sessions.get(previous_digest)
                if previous is not None and previous.revoked_at is None:
                    self._state.web_sessions[previous_digest] = previous.model_copy(
                        update={"revoked_at": now}
                    )
            self._state.web_sessions[candidate.session_digest] = candidate
            return candidate

    async def get_session(self, *, lookup: WebSessionLookup) -> WebSession:
        async with self._lock:
            session = self._state.web_sessions.get(lookup.session_digest)
            if (
                session is None
                or session.revoked_at is not None
                or self._clock() >= session.expires_at
            ):
                raise WebSessionNotFoundError
            return session

    async def revoke_session(self, *, command: RevokeWebSessionCommand) -> bool:
        async with self._lock:
            session = self._state.web_sessions.get(command.session_digest)
            if session is None or session.revoked_at is not None:
                return False
            self._state.web_sessions[session.session_digest] = session.model_copy(
                update={"revoked_at": self._clock()}
            )
            return True


class InMemoryTaskStore:
    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
        task_failure_limit: int = 3,
    ) -> None:
        self._clock = clock
        self._task_failure_limit = validate_task_failure_limit(task_failure_limit)
        self._state = InMemoryPersistenceState() if state is None else state
        self._records = self._state.tasks
        self._by_key = self._state.task_ids_by_key
        # 两张 append-only 台账都按 task_id 分桶，序号是桶内下标 + 1。
        # 不用扁平 list：PostgreSQL 那边的主键是 ``(task_id, seq)``，扁平结构没有
        # "本任务第几条"这个概念，两个实现会对同一次写入给出不同的序号。
        self._approvals = self._state.approvals
        self._audit_events = self._state.audit_events
        self._step_executions = self._state.step_executions
        self._lock = self._state.lock

    def _require(self, task_id: str) -> TaskRecord:
        try:
            return self._records[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id=task_id) from exc

    async def create_task(self, *, submission: TaskSubmission) -> TaskRecord:
        envelope = submission.envelope
        context = submission.context
        # 先校验上下文一致性，再谈幂等：不一致时连"属于哪个作用域"都不成立。
        if not context_matches_envelope(envelope, context):
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
                created_seq=self._state.next_created_seq,
                attempt_number=0,
                task_failure_count=0,
                next_attempt_at=None,
            )
            self._state.next_created_seq += 1
            self._records[record.task_id] = record
            self._state.submissions[record.task_id] = submission
            self._state.submission_digests[record.task_id] = submission_digest(submission)
            self._by_key[scope] = record.task_id
            return record

    async def get(self, *, lookup: TaskLookup) -> TaskRecord:
        current = self._require(lookup.task_id)
        if (
            current.tenant_id != lookup.tenant_id
            or current.environment_id != lookup.environment_id
        ):
            raise TaskNotFoundError(task_id=lookup.task_id)
        return current

    def _stored_read(self, record: TaskRecord) -> StoredTaskRead:
        submission = self._state.submissions.get(record.task_id)
        stored_digest = self._state.submission_digests.get(record.task_id)
        if (
            submission is None
            or stored_digest is None
            or not submission_matches_record(
                record, submission, stored_digest=stored_digest
            )
        ):
            raise TaskNotFoundError(task_id=record.task_id)
        return StoredTaskRead(record=record, submission=submission)

    async def get_submission(self, *, lookup: TaskLookup) -> TaskSubmission:
        async with self._lock:
            current = self._require(lookup.task_id)
            if (
                current.tenant_id != lookup.tenant_id
                or current.environment_id != lookup.environment_id
            ):
                raise TaskNotFoundError(task_id=lookup.task_id)
            return self._stored_read(current).submission

    def _page(
        self,
        *,
        tenant_id: str,
        environment_id: str,
        actor: str | None,
        before_created_seq: int | None,
        limit: int,
    ) -> StoredTaskPage:
        candidates = sorted(
            (
                record
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.environment_id == environment_id
                and (actor is None or record.actor == actor)
                and (
                    before_created_seq is None
                    or record.created_seq < before_created_seq
                )
            ),
            key=lambda record: (record.created_seq, record.task_id),
            reverse=True,
        )
        selected = candidates[: limit + 1]
        items = tuple(self._stored_read(record) for record in selected[:limit])
        next_created_seq = (
            items[-1].record.created_seq if len(selected) > limit else None
        )
        return StoredTaskPage(items=items, next_created_seq=next_created_seq)

    async def list_tasks_for_actor(
        self, *, query: ActorTaskPageQuery
    ) -> StoredTaskPage:
        async with self._lock:
            return self._page(
                tenant_id=query.tenant_id,
                environment_id=query.environment_id,
                actor=query.actor,
                before_created_seq=query.before_created_seq,
                limit=query.limit,
            )

    async def list_tasks_for_scope(
        self, *, query: ScopeTaskPageQuery
    ) -> StoredTaskPage:
        async with self._lock:
            return self._page(
                tenant_id=query.tenant_id,
                environment_id=query.environment_id,
                actor=None,
                before_created_seq=query.before_created_seq,
                limit=query.limit,
            )

    async def transition(
        self, *, command: TransitionCommand
    ) -> TransitionResult:
        async with self._lock:
            current = self._require(command.task_id)
            # ``command.expected_version`` 来自调用方，``current`` 来自存储——两侧必须
            # 异源，否则版本检查恒真。
            rejection = classify_transition(current, command, now=self._clock())
            if rejection is not None:
                return TransitionResult(applied=False, winner=current, rejection=rejection)
            updated = apply_transition(current, command)
            self._records[command.task_id] = updated
            for event in command.audit_events:
                self._append(self._audit_events, command.task_id, event)
            return TransitionResult(applied=True, winner=updated, rejection=None)

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        command = LeaseCommand(task_id=task_id, owner=owner, ttl_seconds=ttl_seconds)
        task_id, owner, ttl_seconds = command.task_id, command.owner, command.ttl_seconds
        async with self._lock:
            current = self._require(task_id)
            if not may_acquire_lease(current, owner=owner, now=self._clock()):
                return None
            token = self._state.next_fencing_token
            expires = self._clock() + _dt.timedelta(seconds=ttl_seconds)
            # **先构造并校验，再写入**：反过来会在参数非法时先污染 store，再抛
            # ValidationError，留下一个带着无效租约字段的任务记录。
            grant = LeaseGrant(
                task_id=task_id, owner=owner, expires_at=expires, fencing_token=token
            )
            updated = current.model_copy(
                update={
                    "lease_owner": owner,
                    "lease_expires_at": expires,
                    "fencing_token": token,
                }
            )
            self._state.next_fencing_token += 1
            self._records[task_id] = updated
            return grant

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> LeaseGrant | None:
        command = LeaseCommand(
            task_id=task_id, owner=owner, ttl_seconds=ttl_seconds, fencing_token=fencing_token
        )
        task_id, owner = command.task_id, command.owner
        ttl_seconds = command.ttl_seconds
        fencing_token = command.fencing_token if command.fencing_token is not None else 0
        async with self._lock:
            current = self._require(task_id)
            if not may_renew_lease(
                current, owner=owner, fencing_token=fencing_token, now=self._clock()
            ):
                return None
            expires = self._clock() + _dt.timedelta(seconds=ttl_seconds)
            grant = LeaseGrant(
                task_id=task_id,
                owner=owner,
                expires_at=expires,
                fencing_token=fencing_token,
            )
            self._records[task_id] = current.model_copy(update={"lease_expires_at": expires})
            return grant

    async def list_stale_leases(
        self, *, query: StaleLeaseQuery
    ) -> tuple[TaskRecord, ...]:
        async with self._lock:
            now = self._clock()
            stale = [r for r in self._records.values() if is_stale_lease(r, now=now)]
        return tuple(sorted(stale, key=stale_lease_sort_key)[: query.limit])

    @staticmethod
    def _append(
        ledger: dict[str, list[_EntryT]], task_id: str, entry: _EntryT
    ) -> int:
        """append-only 追加，返回本任务内的 ``seq``（从 1 开始）。

        两张台账逐字共用同一段分配逻辑，理由与判定纯函数相同：分成两份就会分叉，
        而"审批序号和审计序号编号方式不一样"不会让任何用例失败。
        """
        bucket = ledger.setdefault(task_id, [])
        bucket.append(entry)
        return len(bucket)

    async def record_approval(self, *, request: ApprovalRequest) -> int:
        async with self._lock:
            return self._append(self._approvals, request.task_id, request)

    async def record_audit_event(self, *, event: TraceEvent) -> int:
        # 先拒绝再上锁：不带 task_id 的事件连"记到哪个桶"都不成立，没有任何理由
        # 让它进入临界区。
        if event.task_id is None:
            raise UnscopedAuditEventError("audit event has no task_id")
        async with self._lock:
            return self._append(self._audit_events, event.task_id, event)

    async def list_dispatchable_tasks(
        self, *, query: DispatchQuery
    ) -> tuple[TaskRecord, ...]:
        async with self._lock:
            now = self._clock()
            candidates = [
                record
                for record in self._records.values()
                if is_dispatchable(record, query, now=now)
            ]
        return tuple(sorted(candidates, key=dispatch_sort_key)[: query.limit])

    def _terminalize_attempt_rejection(
        self,
        *,
        current: TaskRecord,
        command: TaskAttemptCommand,
        rejection: TaskAttemptRejection,
        now: _dt.datetime,
    ) -> TaskAttemptResult:
        winner = current.model_copy(
            update={
                "status": TaskStatus.FAILED,
                "version": current.version + 1,
                "terminal_reason": rejection.value,
            }
        )
        event = attempt_terminal_event(command=command, winner=winner, now=now)
        self._records[current.task_id] = winner
        self._append(self._audit_events, current.task_id, event)
        return TaskAttemptResult(
            applied=False,
            winner=winner,
            rejection=rejection,
            committed_audit_events=(event,),
        )

    async def begin_task_attempt(
        self, *, command: TaskAttemptCommand
    ) -> TaskAttemptResult:
        async with self._lock:
            current = self._require(command.task_id)
            now = self._clock()
            rejection = classify_task_attempt(
                current,
                intent=command.intent,
                now=now,
                task_failure_limit=self._task_failure_limit,
            )
            if rejection is TaskAttemptRejection.RETRY_EXHAUSTED:
                return self._terminalize_attempt_rejection(
                    current=current,
                    command=command,
                    rejection=rejection,
                    now=now,
                )
            if rejection is not None:
                return TaskAttemptResult(
                    applied=False, winner=current, rejection=rejection
                )

            submission = self._state.submissions.get(current.task_id)
            stored_digest = self._state.submission_digests.get(current.task_id)
            if (
                submission is None
                or stored_digest is None
                or not submission_matches_record(
                    current, submission, stored_digest=stored_digest
                )
            ):
                return self._terminalize_attempt_rejection(
                    current=current,
                    command=command,
                    rejection=TaskAttemptRejection.SUBMISSION_INVARIANT_VIOLATION,
                    now=now,
                )

            token = self._state.next_fencing_token
            expires_at = now + _dt.timedelta(seconds=command.ttl_seconds)
            winner = current.model_copy(
                update={
                    "attempt_number": current.attempt_number + 1,
                    "next_attempt_at": None,
                    "lease_owner": command.owner,
                    "lease_expires_at": expires_at,
                    "fencing_token": token,
                }
            )
            grant = TaskAttemptGrant(
                lease=LeaseGrant(
                    task_id=current.task_id,
                    owner=command.owner,
                    expires_at=expires_at,
                    fencing_token=token,
                ),
                attempt_number=winner.attempt_number,
            )
            self._records[current.task_id] = winner
            self._state.next_fencing_token += 1
            return TaskAttemptResult(
                applied=True,
                winner=winner,
                grant=grant,
                submission=submission,
            )

    async def schedule_retry(self, *, command: RetryCommand) -> RetryResult:
        digest = retry_command_digest(command)
        async with self._lock:
            current = self._require(command.grant.task_id)
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

            grant_rejection = grant_is_current(
                current,
                command.grant,
                now=self._clock(),
                allowed_statuses=frozenset(
                    {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}
                ),
            )
            if grant_rejection is not None:
                return RetryResult(decision=RetryDecision.STALE_FENCING, winner=current)

            token = self._state.next_fencing_token
            winner = current.model_copy(
                update={
                    "version": current.version + 1,
                    "task_failure_count": current.task_failure_count + 1,
                    "next_attempt_at": command.next_attempt_at,
                    "retry_scheduled_by_attempt": command.grant.attempt_number,
                    "retry_command_digest": digest,
                    "lease_expires_at": command.next_attempt_at,
                    "fencing_token": token,
                }
            )
            self._records[current.task_id] = winner
            self._state.next_fencing_token += 1
            for event in command.audit_events:
                self._append(self._audit_events, current.task_id, event)
            return RetryResult(decision=RetryDecision.SCHEDULED, winner=winner)

    def _attempts_used(self, task_id: str) -> int:
        return sum(
            record.attempt_count
            for (stored_task_id, _), record in self._step_executions.items()
            if stored_task_id == task_id
        )

    async def begin_step_attempt(
        self, *, command: StepAttemptCommand
    ) -> StepAttemptResult:
        async with self._lock:
            current = self._require(command.grant.task_id)
            key = (current.task_id, command.step_id)
            existing = self._step_executions.get(key)
            attempts_used = self._attempts_used(current.task_id)
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

            stored = self._state.plans.get(current.task_id)
            if stored is None:
                return StepAttemptResult(
                    decision=StepAttemptDecision.NOT_RUNNABLE,
                    winner=current,
                    record=existing,
                    attempts_used=attempts_used,
                )
            step_ids = {step.step_id for step in stored.plan.steps}
            if command.step_id not in step_ids:
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
            if (
                increments_budget
                and attempts_used >= stored.plan.budget.max_tool_calls
            ):
                return StepAttemptResult(
                    decision=StepAttemptDecision.BUDGET_EXHAUSTED,
                    winner=current,
                    record=existing,
                    attempts_used=attempts_used,
                )

            if existing is None:
                record = StepExecutionRecord(
                    task_id=current.task_id,
                    step_id=command.step_id,
                    attempt_count=1,
                    last_fencing_token=command.grant.fencing_token,
                    started_at=self._clock(),
                )
            elif increments_budget:
                record = existing.model_copy(
                    update={
                        "attempt_count": existing.attempt_count + 1,
                        "last_fencing_token": command.grant.fencing_token,
                        "started_at": self._clock(),
                    }
                )
            else:
                record = existing
            self._step_executions[key] = record
            return StepAttemptResult(
                decision=StepAttemptDecision.PROCEED,
                winner=current,
                record=record,
                attempts_used=self._attempts_used(current.task_id),
            )

    async def commit_step_result(
        self, *, command: StepCommitCommand
    ) -> StepCommitResult:
        digest = step_commit_digest(command)
        async with self._lock:
            current = self._require(command.grant.task_id)
            key = (current.task_id, command.step_id)
            existing = self._step_executions.get(key)
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

            stored = self._state.plans.get(current.task_id)
            if stored is None:
                raise ValueError("step commit has no stored plan")
            evidence = command.evidence
            if evidence is not None and (
                evidence.capability_id != stored.plan.capability_id
                or evidence.capability_version != stored.plan.capability_version
            ):
                raise ValueError("step evidence does not match the stored plan")
            if evidence is not None:
                evidence_bucket = self._state.evidence.setdefault(current.task_id, {})
                previous = evidence_bucket.get(evidence.evidence_id)
                if previous is not None and previous != evidence:
                    raise ValueError("different evidence already exists for this step")

            committed = StepExecutionRecord(
                task_id=existing.task_id,
                step_id=existing.step_id,
                attempt_count=existing.attempt_count,
                last_fencing_token=existing.last_fencing_token,
                result_status=command.status,
                kind=command.kind,
                evidence_id=None if evidence is None else evidence.evidence_id,
                commit_digest=digest,
                started_at=existing.started_at,
                committed_at=self._clock(),
            )
            if evidence is not None:
                self._state.evidence[current.task_id][evidence.evidence_id] = evidence
            for event in command.audit_events:
                self._append(self._audit_events, current.task_id, event)
            self._step_executions[key] = committed
            return StepCommitResult(
                committed=True, winner=current, record=committed
            )

    async def load_step_executions(
        self, *, task_id: str
    ) -> tuple[StepExecutionRecord, ...]:
        async with self._lock:
            records = [
                record
                for (stored_task_id, _), record in self._step_executions.items()
                if stored_task_id == task_id
            ]
        return tuple(sorted(records, key=lambda record: record.step_id))

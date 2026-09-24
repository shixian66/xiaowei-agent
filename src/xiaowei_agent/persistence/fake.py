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
from collections.abc import Mapping
from typing import Final, TypeVar

from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ActorTaskPageQuery,
    ApprovalRequest,
    ChannelKind,
    GrantRejection,
    IdentitySource,
    LeaseGrant,
    LoadReceipt,
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
    TestResult,
    TraceEvent,
    TransitionResult,
)
from xiaowei_agent.contracts.activation import (
    ActivationLookup,
    ActivationRequest,
    ActivationStatus,
    CreateActivationCommand,
    PendingActivationListQuery,
    PendingActivationPage,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEvent,
    AdminAuditListQuery,
    AdminAuditPage,
    AdminAuditStart,
    AdminAuditTerminal,
    AdminOperationContext,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditOutcome,
    AdminAuditReasonCode,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AdminUserListQuery,
    AdminUserPage,
    AdminUserRecord,
    ApproveActivationCommand,
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    DirectoryCommand,
    DirectoryPrincipalFacts,
    ExternalIdentity,
    MigrateLegacyIdentitiesCommand,
    RejectActivationCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
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
    AuditStage,
    audit_stage_key,
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
from xiaowei_agent.persistence.local_admin import (
    LOCAL_ADMIN_SUBJECT_REF,
    ChangePasswordCommand,
    LocalAdminNotFoundError,
    LocalAdminRecord,
    bootstrap_local_admin_command,
    bootstrap_operation_context,
)
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.provider_state import (
    ProviderStateSnapshot,
    RecordTestCommand,
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
    ConsumeOAuthLoginStateCommand,
    ConsumeOAuthStateCommand,
    IssueOAuthLoginStateCommand,
    IssueOAuthStateCommand,
    OAuthLoginContextNotFoundError,
    OAuthLoginState,
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

    async def get_binding(self, *, lookup: ChannelBindingLookup) -> ChannelBinding:
        async with self._lock:
            binding_id = self._state.channel_binding_ids_by_task.get(lookup.task_id)
            if binding_id is None:
                raise ChannelBindingNotFoundError
            binding = self._state.channel_bindings[binding_id]
            if (
                binding.tenant_id != lookup.tenant_id
                or binding.environment_id != lookup.environment_id
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


class InMemoryProviderStateStore:
    """``ProviderStateStore`` 的单进程实现；与其它端口共用同一把锁。"""

    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
    ) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    async def record_load(
        self, *, receipts: Mapping[tuple[str, str], LoadReceipt]
    ) -> None:
        if not receipts:
            # 空映射不是"清空"：旧回执仍是"上次加载了第几代"的事实。
            return
        async with self._lock:
            self._state.load_receipts.update(receipts)

    async def record_test(self, *, command: RecordTestCommand) -> None:
        async with self._lock:
            self._state.provider_tests[command.check_name.value] = TestResult(
                status=command.status,
                generation=command.generation,
                tested_at=self._clock(),
            )

    async def snapshot(self) -> ProviderStateSnapshot:
        async with self._lock:
            return ProviderStateSnapshot(
                receipts=self._state.load_receipts,
                tests=self._state.provider_tests,
            )


class InMemoryActivationStore:
    """``ActivationStore`` 的单进程实现；与目录 Store 共用状态与锁。"""

    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
    ) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    async def create_or_reuse(
        self, *, command: CreateActivationCommand
    ) -> ActivationRequest:
        async with self._lock:
            now = self._clock()
            for request_id, request in tuple(self._state.activation_requests.items()):
                if (
                    request.status is ActivationStatus.PENDING
                    and request.expires_at <= now
                ):
                    self._state.activation_requests[request_id] = request.model_copy(
                        update={"status": ActivationStatus.EXPIRED}
                    )

            pending = sum(
                request.status is ActivationStatus.PENDING
                for request in self._state.activation_requests.values()
            )
            if pending >= MAX_PENDING_ACTIVATIONS:
                raise ActivationCapacityError

            subject_digest = activation_subject_digest(command)
            for request in self._state.activation_requests.values():
                if (
                    request.status is ActivationStatus.PENDING
                    and request.tenant_id == command.tenant_id
                    and request.environment_id == command.environment_id
                    and request.provider is command.provider
                    and request.subject_ref_digest == subject_digest
                ):
                    return request

            event_digest = (
                None
                if command.source_event_ref is None
                else activation_source_ref_digest(
                    kind="event", reference=command.source_event_ref
                )
            )
            chat_digest = (
                None
                if command.source_chat_ref is None
                else activation_source_ref_digest(
                    kind="chat", reference=command.source_chat_ref
                )
            )
            created = ActivationRequest(
                request_id=str(uuid.uuid4()),
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                provider=command.provider,
                subject_ref=command.subject_ref,
                subject_ref_digest=subject_digest,
                source=command.source,
                return_intent=command.return_intent,
                source_event_digest=event_digest,
                source_chat_digest=chat_digest,
                requested_at=now,
                expires_at=now + _dt.timedelta(seconds=ACTIVATION_TTL_SECONDS),
                status=ActivationStatus.PENDING,
            )
            self._state.activation_requests[created.request_id] = created
            return created

    async def load(self, *, query: ActivationLookup) -> ActivationRequest | None:
        async with self._lock:
            request = self._state.activation_requests.get(query.request_id)
            if request is None:
                return None
            if (
                request.tenant_id != query.tenant_id
                or request.environment_id != query.environment_id
            ):
                return None
            return request

    async def list_pending(
        self, *, query: PendingActivationListQuery
    ) -> PendingActivationPage:
        """只返回显式作用域内仍有效的 pending 申请。"""
        async with self._lock:
            now = self._clock()
            cursor = (
                None
                if query.before_requested_at is None
                else (query.before_requested_at, query.before_request_id)
            )
            candidates = sorted(
                (
                    request
                    for request in self._state.activation_requests.values()
                    if request.tenant_id == query.tenant_id
                    and request.environment_id == query.environment_id
                    and request.status is ActivationStatus.PENDING
                    and request.expires_at > now
                    and (
                        cursor is None
                        or (request.requested_at, request.request_id) < cursor
                    )
                ),
                key=lambda request: (request.requested_at, request.request_id),
                reverse=True,
            )
            items = tuple(candidates[: query.limit])
            has_more = len(candidates) > query.limit
            tail = items[-1] if has_more else None
            return PendingActivationPage(
                items=items,
                next_requested_at=None if tail is None else tail.requested_at,
                next_request_id=None if tail is None else tail.request_id,
            )


class InMemoryAdminAuditStore:
    """``AdminAuditStore`` 的单进程实现；与目录 store **共用同一把锁和同一份事实**。

    共用是承重的：授权改变与它的审计必须在同一个临界区里提交，两边各持一份状态
    就复现不出这条不变量，而它恰好是 W1a 的核心。
    """

    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
    ) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    def _append_locked(
        self, candidate: AdminAuditCandidate, *, now: _dt.datetime
    ) -> AdminAuditEvent | None:
        """**唯一**往内存审计事实里写的地方；阶段已被占用时返回 ``None``。

        返回 ``None`` 而不是自己抛：同一个事实经两个公开入口暴露时是两个错误族
        （``AdminAuditConflictError`` 与 ``AdminAuditUnwritableError``），由调用者
        按自己的入口决定抛哪个。这与 PostgreSQL 侧 ``_insert_audit_event`` 的
        ``ON CONFLICT DO NOTHING ... RETURNING`` 拿到空结果是同一个形状。
        """
        key = audit_stage_key(
            operation_id=candidate.operation_id, outcome=candidate.outcome
        )
        if key in self._state.admin_audit_stage_keys:
            return None
        event = seal(candidate, now=now)
        if event.event_id in self._state.admin_audit_events:
            return None
        self._state.admin_audit_stage_keys[key] = event.event_id
        self._state.admin_audit_events[event.event_id] = event
        return event

    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent:
        candidate = AdminAuditCandidate(
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
        async with self._lock:
            event = self._append_locked(candidate, now=self._clock())
        if event is None:
            raise AdminAuditConflictError
        return event

    async def append_terminal(
        self, *, terminal: AdminAuditTerminal
    ) -> AdminAuditEvent:
        async with self._lock:
            started_id = self._state.admin_audit_stage_keys.get(
                audit_stage_key(
                    operation_id=terminal.operation_id,
                    outcome=AdminAuditOutcome.STARTED,
                )
            )
            if started_id is None:
                raise AdminAuditMissingStartError
            started = self._state.admin_audit_events[started_id]
            # 稳定字段从已存的 STARTED 读回，不由调用方再传一遍——再传一遍就等于
            # 给了调用方一次改写租户、操作者或目标的机会。
            candidate = AdminAuditCandidate(
                operation_id=started.operation_id,
                tenant_id=started.tenant_id,
                environment_id=started.environment_id,
                actor_user_id=started.actor_user_id,
                actor=started.actor,
                auth_source=started.auth_source,
                action=started.action,
                target_kind=started.target_kind,
                target_ref_digest=started.target_ref_digest,
                outcome=terminal.outcome,
                reason_code=terminal.reason_code,
                effect=terminal.effect,
            )
            event = self._append_locked(candidate, now=self._clock())
        if event is None:
            raise AdminAuditConflictError
        return event

    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent:
        candidate = AdminAuditCandidate(
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
        async with self._lock:
            event = self._append_locked(candidate, now=self._clock())
        if event is None:
            raise AdminAuditConflictError
        return event

    async def load(self, *, event_id: str) -> AdminAuditEvent | None:
        async with self._lock:
            return self._state.admin_audit_events.get(event_id)

    async def list_events(self, *, query: AdminAuditListQuery) -> AdminAuditPage:
        """按显式作用域、闭集过滤与复合 keyset 读取审计事实。"""
        async with self._lock:
            cursor = (
                None
                if query.before_created_at is None
                else (query.before_created_at, query.before_event_id)
            )
            candidates = sorted(
                (
                    event
                    for event in self._state.admin_audit_events.values()
                    if event.tenant_id == query.tenant_id
                    and event.environment_id == query.environment_id
                    and (query.action is None or event.action is query.action)
                    and (query.outcome is None or event.outcome is query.outcome)
                    and (
                        query.target_kind is None
                        or (
                            event.target_kind is query.target_kind
                            and event.target_ref_digest == query.target_ref_digest
                        )
                    )
                    and (
                        cursor is None or (event.created_at, event.event_id) < cursor
                    )
                ),
                key=lambda event: (event.created_at, event.event_id),
                reverse=True,
            )
            items = tuple(candidates[: query.limit])
            has_more = len(candidates) > query.limit
            tail = items[-1] if has_more else None
            return AdminAuditPage(
                items=items,
                next_created_at=None if tail is None else tail.created_at,
                next_event_id=None if tail is None else tail.event_id,
            )


_DirectorySnapshot = tuple[
    dict[str, UserAccount],
    dict[tuple[str, str, str], UserRoleAssignment],
    dict[tuple[str, str, str, str], ExternalIdentity],
    dict[str, AdminAuditEvent],
    dict[tuple[str, AuditStage], str],
    dict[str, ActivationRequest],
    object,
]


class InMemoryUserDirectoryStore:
    """``UserDirectoryStore`` 的单进程实现。

    没有事务，用"进临界区先拍快照、**任何**异常先恢复再决定抛什么"表达回滚那一列。
    不挑异常类型恢复：只在两三种异常上恢复，审计派生、契约校验或 helper 抛出的
    别的异常就会留下"授权已改、审计没写"的半状态，两个实现在共享套件**之外**分叉。
    """

    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
    ) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock
        self._audit = InMemoryAdminAuditStore(clock=clock, state=self._state)

    async def load_account(
        self, *, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None:
        async with self._lock:
            return self._facts(user_id, tenant_id, environment_id)

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
        async with self._lock:
            binding = self._state.external_identities.get(
                (provider.value, tenant_id, environment_id, digest)
            )
            if binding is None:
                return None
            facts = self._facts(binding.user_id, tenant_id, environment_id)
            if facts is None:
                raise UserDirectorySubjectUnavailableError(
                    "the bound subject is unavailable"
                )
            return facts

    async def list_admin_users(self, *, query: AdminUserListQuery) -> AdminUserPage:
        """一次批量读取当前作用域角色、账号状态与飞书绑定事实。"""
        async with self._lock:
            candidates: list[AdminUserRecord] = []
            for (user_id, tenant_id, environment_id), assignment in (
                self._state.user_role_assignments.items()
            ):
                if tenant_id != query.tenant_id or environment_id != query.environment_id:
                    continue
                account = self._state.user_accounts[user_id]
                if query.after_actor is not None and account.actor <= query.after_actor:
                    continue
                bound = any(
                    binding.user_id == user_id
                    and binding.tenant_id == query.tenant_id
                    and binding.environment_id == query.environment_id
                    and binding.provider is IdentitySource.FEISHU
                    for binding in self._state.external_identities.values()
                )
                candidates.append(
                    AdminUserRecord(
                        account=account,
                        assignment=assignment,
                        feishu_bound=bound,
                    )
                )
            candidates.sort(key=lambda item: item.account.actor)
            items = tuple(candidates[: query.limit])
            has_more = len(candidates) > query.limit
            return AdminUserPage(
                items=items,
                next_after_actor=(items[-1].account.actor if has_more else None),
            )

    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        async with self._lock:
            snapshot = self._snapshot()
            try:
                return self._apply_locked(command=command, context=context)
            except BaseException:
                self._restore(snapshot)
                raise

    # ---- 读 ----------------------------------------------------------------

    def _facts(
        self, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None:
        """停用的账号解析不出任何授权事实——停用即刻生效，不等下一次登录。"""
        account = self._state.user_accounts.get(user_id)
        if account is None or account.status is not UserStatus.ACTIVE:
            return None
        assignment = self._state.user_role_assignments.get(
            (user_id, tenant_id, environment_id)
        )
        if assignment is None:
            return None
        return DirectoryPrincipalFacts(account=account, assignment=assignment)

    # ---- 快照 --------------------------------------------------------------

    def _snapshot(self) -> _DirectorySnapshot:
        """四张目录表 **加上** 凭据行。

        漏掉凭据行时，一次失败的 bootstrap 会留下一行 ``user_id IS NULL`` 的凭据，
        而那正好落进四态表第二行（backfill）而不是第四行（fail-closed）——于是它
        看起来像一次"还没升级"的正常状态。
        """
        state = self._state
        return (
            dict(state.user_accounts),
            dict(state.user_role_assignments),
            dict(state.external_identities),
            dict(state.admin_audit_events),
            dict(state.admin_audit_stage_keys),
            dict(state.activation_requests),
            state.local_admin,
        )

    def _restore(self, snapshot: _DirectorySnapshot) -> None:
        state = self._state
        (
            accounts,
            assignments,
            identities,
            events,
            stage_keys,
            activations,
            local_admin,
        ) = snapshot
        # 逐个写而不是在一个循环里遍历：五张表的键值类型各不相同，塞进一个循环
        # 就只能靠一条静音注释把类型错误压下去，而被压下去的那一条正是"恢复错了
        # 表"这种缺陷唯一会留下的信号。
        state.user_accounts.clear()
        state.user_accounts.update(accounts)
        state.user_role_assignments.clear()
        state.user_role_assignments.update(assignments)
        state.external_identities.clear()
        state.external_identities.update(identities)
        state.admin_audit_events.clear()
        state.admin_audit_events.update(events)
        state.admin_audit_stage_keys.clear()
        state.admin_audit_stage_keys.update(stage_keys)
        state.activation_requests.clear()
        state.activation_requests.update(activations)
        state.local_admin = local_admin

    # ---- 写 ----------------------------------------------------------------

    def _apply_locked(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        now = self._clock()
        if isinstance(command, CreateUserCommand):
            return self._create_user(command, context, now)
        if isinstance(command, SetUserStatusCommand):
            return self._set_status(command, context, now)
        if isinstance(command, AssignRoleCommand):
            return self._assign_role(command, context, now)
        if isinstance(command, RevokeRoleCommand):
            return self._revoke_role(command, context, now)
        if isinstance(command, BindExternalIdentityCommand):
            return self._bind_identity(command, context, now)
        if isinstance(command, UnbindExternalIdentityCommand):
            return self._unbind_identity(command, context, now)
        if isinstance(command, BootstrapLocalAdminCommand):
            return self._bootstrap(command, context, now)
        if isinstance(command, MigrateLegacyIdentitiesCommand):
            return self._migrate(command, context, now)
        if isinstance(command, ApproveActivationCommand):
            return self._approve_activation(command, context, now)
        return self._reject_activation(command, context, now)

    def _write_audit(
        self, candidate: AdminAuditCandidate, *, now: _dt.datetime
    ) -> AdminAuditEvent:
        event = self._audit._append_locked(candidate, now=now)
        if event is None:
            raise AdminAuditUnwritableError
        return event

    def _insert_account(
        self,
        *,
        user_id: str,
        actor: str,
        display_name: str,
        now: _dt.datetime,
    ) -> None:
        if user_id in self._state.user_accounts:
            raise UserDirectoryConflictError("the account already exists")
        if any(
            account.actor == actor for account in self._state.user_accounts.values()
        ):
            raise UserDirectoryConflictError("the actor is already claimed")
        self._state.user_accounts[user_id] = UserAccount(
            user_id=user_id,
            actor=actor,
            display_name=display_name,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )

    def _upsert_role(
        self,
        *,
        user_id: str,
        tenant_id: str,
        environment_id: str,
        role: ProductRole,
        created_by: str,
        now: _dt.datetime,
    ) -> None:
        key = (user_id, tenant_id, environment_id)
        existing = self._state.user_role_assignments.get(key)
        self._state.user_role_assignments[key] = UserRoleAssignment(
            user_id=user_id,
            tenant_id=tenant_id,
            environment_id=environment_id,
            role=role,
            created_by=created_by if existing is None else existing.created_by,
            created_at=now if existing is None else existing.created_at,
            updated_at=now,
        )

    def _bind_subject(
        self,
        *,
        user_id: str,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
        now: _dt.datetime,
    ) -> None:
        digest = external_subject_digest(
            provider=IdentitySource.FEISHU,
            tenant_id=tenant_id,
            environment_id=environment_id,
            subject_ref=subject_ref,
        )
        key = (IdentitySource.FEISHU.value, tenant_id, environment_id, digest)
        if key in self._state.external_identities:
            raise UserDirectoryConflictError("the subject is already bound")
        # 反方向也要挡：一个账号在同作用域最多绑一个主体。只挡主键时，一个账号
        # 可以绑上任意多个 open_id，于是"撤销这个人"要撤几次没有定论。
        if self._binding_key_of(user_id, tenant_id, environment_id) is not None:
            raise UserDirectoryConflictError("the account is already bound")
        self._state.external_identities[key] = ExternalIdentity(
            user_id=user_id,
            provider=IdentitySource.FEISHU,
            tenant_id=tenant_id,
            environment_id=environment_id,
            subject_ref=subject_ref,
            created_at=now,
            last_seen_at=now,
        )

    def _binding_key_of(
        self, user_id: str, tenant_id: str, environment_id: str
    ) -> tuple[str, str, str, str] | None:
        for key, binding in self._state.external_identities.items():
            if (
                binding.user_id == user_id
                and key[1] == tenant_id
                and key[2] == environment_id
            ):
                return key
        return None

    def _require_account(self, user_id: str) -> UserAccount:
        account = self._state.user_accounts.get(user_id)
        if account is None:
            raise UserDirectoryNotFoundError("no such account")
        return account

    def _create_user(
        self,
        command: CreateUserCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._insert_account(
            user_id=command.user_id,
            actor=command.actor,
            display_name=command.display_name,
            now=now,
        )
        self._upsert_role(
            user_id=command.user_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            role=command.role,
            created_by=context.actor_user_id,
            now=now,
        )
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _set_status(
        self,
        command: SetUserStatusCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        account = self._require_account(command.user_id)
        # 作用域里没有角色的账号在这个作用域根本不可见，改它的状态等于隔着作用域
        # 动一个看不到的人。
        if (
            command.user_id,
            command.tenant_id,
            command.environment_id,
        ) not in self._state.user_role_assignments:
            raise UserDirectoryNotFoundError("no role in this scope")
        self._state.user_accounts[command.user_id] = account.model_copy(
            update={"status": command.status, "updated_at": now}
        )
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _assign_role(
        self,
        command: AssignRoleCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._require_account(command.user_id)
        self._upsert_role(
            user_id=command.user_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            role=command.role,
            created_by=context.actor_user_id,
            now=now,
        )
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _revoke_role(
        self,
        command: RevokeRoleCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._require_account(command.user_id)
        key = (command.user_id, command.tenant_id, command.environment_id)
        if key not in self._state.user_role_assignments:
            raise UserDirectoryNotFoundError("no role in this scope")
        del self._state.user_role_assignments[key]
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _bind_identity(
        self,
        command: BindExternalIdentityCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._require_account(command.user_id)
        self._bind_subject(
            user_id=command.user_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            subject_ref=command.subject_ref,
            now=now,
        )
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _unbind_identity(
        self,
        command: UnbindExternalIdentityCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._require_account(command.user_id)
        key = self._binding_key_of(
            command.user_id, command.tenant_id, command.environment_id
        )
        if key is None:
            raise UserDirectoryNotFoundError("no binding in this scope")
        del self._state.external_identities[key]
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _bootstrap(
        self,
        command: BootstrapLocalAdminCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        """四态表的四行都在这里；判定读的是**目录链接是否完整**，不是凭据行在不在。

        按"已有凭据行即 no-op"实现，任何跑过 RI5 的库升级之后 ``user_id`` 永远是
        ``NULL``、``user_accounts`` 不会出现、ADMIN 角色不会出现——而全新安装的
        测试全绿，因为全新安装走的是第一行。
        """
        record = self._state.local_admin
        if record is not None and record.user_id is not None:
            account = self._state.user_accounts.get(record.user_id)
            assignment = self._state.user_role_assignments.get(
                (record.user_id, command.tenant_id, command.environment_id)
            )
            if (
                account is None
                or assignment is None
                or assignment.role is not ProductRole.ADMIN
            ):
                raise UserDirectoryConflictError("the credential link is broken")
            return ()
        self._insert_account(
            user_id=command.user_id,
            actor=command.actor,
            display_name=command.display_name,
            now=now,
        )
        self._upsert_role(
            user_id=command.user_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            role=ProductRole.ADMIN,
            created_by=context.actor_user_id,
            now=now,
        )
        # backfill 时**保留原 password_hash**：管理员可能早就改过密码，拿传进来的
        # 初始口令覆盖回去等于一次静默的凭据回滚。命令里的口令只在第一行被使用。
        self._state.local_admin = LocalAdminRecord(
            password_hash=(
                command.password_hash if record is None else record.password_hash
            ),
            must_change_password=(
                True if record is None else record.must_change_password
            ),
            user_id=command.user_id,
        )
        candidate = derive_audit(command, context, target_ref=command.user_id)
        return (self._write_audit(candidate, now=now),)

    def _migrate(
        self,
        command: MigrateLegacyIdentitiesCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        events: list[AdminAuditEvent] = []
        for index, entry in enumerate(command.entries, start=1):
            self._insert_account(
                user_id=entry.user_id,
                actor=entry.actor,
                display_name=entry.display_name,
                now=now,
            )
            self._upsert_role(
                user_id=entry.user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                role=entry.role,
                created_by=context.actor_user_id,
                now=now,
            )
            self._bind_subject(
                user_id=entry.user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                subject_ref=entry.subject_ref,
                now=now,
            )
            candidate = derive_audit(
                command,
                context,
                target_ref=entry.user_id,
                operation_id=batch_operation_id(context, index),
                entry=entry,
            )
            events.append(self._write_audit(candidate, now=now))
        return tuple(events)

    def _require_activation_admin(
        self,
        *,
        tenant_id: str,
        environment_id: str,
        context: AdminOperationContext,
    ) -> None:
        if context.auth_source is not IdentitySource.LOCAL_ADMIN:
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED
            )
        account = self._state.user_accounts.get(context.actor_user_id)
        assignment = self._state.user_role_assignments.get(
            (context.actor_user_id, tenant_id, environment_id)
        )
        if (
            account is None
            or account.status is not UserStatus.ACTIVE
            or account.actor != context.actor
            or assignment is None
            or assignment.role is not ProductRole.ADMIN
        ):
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.ACTOR_NOT_ADMIN
            )

    def _activation_for_decision(
        self,
        *,
        request_id: str,
        tenant_id: str,
        environment_id: str,
    ) -> ActivationRequest:
        request = self._state.activation_requests.get(request_id)
        if request is None:
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.TARGET_NOT_FOUND
            )
        if (
            request.tenant_id != tenant_id
            or request.environment_id != environment_id
        ):
            raise UserDirectoryDecisionDeniedError(
                AdminAuditReasonCode.SCOPE_MISMATCH
            )
        return request

    def _approve_activation(
        self,
        command: ApproveActivationCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._require_activation_admin(
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            context=context,
        )
        request = self._activation_for_decision(
            request_id=command.request_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
        )
        if request.status is not ActivationStatus.PENDING:
            raise UserDirectoryConflictError("the activation request is terminal")
        if request.expires_at <= now:
            raise UserDirectoryConflictError("the activation request expired")
        account = next(
            (
                candidate
                for candidate in self._state.user_accounts.values()
                if candidate.actor == command.actor
            ),
            None,
        )
        if account is not None:
            if (
                account.status is not UserStatus.ACTIVE
                or account.display_name != command.display_name
            ):
                raise UserDirectoryConflictError(
                    "the activation account conflicts"
                )
            user_id = account.user_id
        else:
            user_id = activation_user_id(
                subject_ref_digest=request.subject_ref_digest
            )
            account = self._state.user_accounts.get(user_id)
        if account is None:
            self._insert_account(
                user_id=user_id,
                actor=command.actor,
                display_name=command.display_name,
                now=now,
            )
        elif (
            account.actor != command.actor
            or account.display_name != command.display_name
            or account.status is not UserStatus.ACTIVE
        ):
            raise UserDirectoryConflictError("the activation account conflicts")
        role_key = (user_id, command.tenant_id, command.environment_id)
        assignment = self._state.user_role_assignments.get(role_key)
        if assignment is None:
            self._upsert_role(
                user_id=user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                role=command.approved_role,
                created_by=context.actor_user_id,
                now=now,
            )
        elif assignment.role is not command.approved_role:
            raise UserDirectoryConflictError("the existing role differs")
        binding_key = (
            IdentitySource.FEISHU.value,
            command.tenant_id,
            command.environment_id,
            external_subject_digest(
                provider=IdentitySource.FEISHU,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                subject_ref=request.subject_ref,
            ),
        )
        binding = self._state.external_identities.get(binding_key)
        if binding is None:
            self._bind_subject(
                user_id=user_id,
                tenant_id=command.tenant_id,
                environment_id=command.environment_id,
                subject_ref=request.subject_ref,
                now=now,
            )
        elif binding.user_id != user_id:
            raise UserDirectoryConflictError("the activation subject conflicts")
        self._state.activation_requests[request.request_id] = request.model_copy(
            update={
                "status": ActivationStatus.APPROVED,
                "decided_at": now,
                "decided_by": context.actor_user_id,
                "approved_role": command.approved_role,
            }
        )
        candidate = derive_audit(command, context, target_ref=request.request_id)
        return (self._write_audit(candidate, now=now),)

    def _reject_activation(
        self,
        command: RejectActivationCommand,
        context: AdminOperationContext,
        now: _dt.datetime,
    ) -> tuple[AdminAuditEvent, ...]:
        self._require_activation_admin(
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
            context=context,
        )
        request = self._activation_for_decision(
            request_id=command.request_id,
            tenant_id=command.tenant_id,
            environment_id=command.environment_id,
        )
        if request.status is not ActivationStatus.PENDING:
            raise UserDirectoryConflictError("the activation request is terminal")
        if request.expires_at <= now:
            raise UserDirectoryConflictError("the activation request expired")
        self._state.activation_requests[request.request_id] = request.model_copy(
            update={
                "status": ActivationStatus.REJECTED,
                "decided_at": now,
                "decided_by": context.actor_user_id,
            }
        )
        candidate = derive_audit(command, context, target_ref=request.request_id)
        return (self._write_audit(candidate, now=now),)


class InMemoryLocalAdminStore:
    """与 session 存储**共用同一把锁和同一份事实**的本地管理员实现。

    共用是承重的：改密要在一次原子操作里同时改口令和撤销全部旧 session，
    两边各持一份状态就复现不出这条不变量。
    """

    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
    ) -> None:
        self._clock = clock
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    async def seed_if_absent(self, *, password_hash: str) -> bool:
        """委托给 ``apply()``，不再自己构造这一行。

        ``local_admins.user_id`` 是授权事实，而授权事实只有一条写路径；凭据行与
        目录账号也必须一起出现，否则失败会留下一行无人链接的凭据。
        """
        directory = InMemoryUserDirectoryStore(clock=self._clock, state=self._state)
        events = await directory.apply(
            command=bootstrap_local_admin_command(password_hash=password_hash),
            context=bootstrap_operation_context(),
        )
        return bool(events)

    async def get(self) -> LocalAdminRecord:
        async with self._lock:
            record = self._state.local_admin
            if not isinstance(record, LocalAdminRecord):
                raise LocalAdminNotFoundError
            return record

    async def change_password_and_rotate_session(
        self, *, command: ChangePasswordCommand
    ) -> LocalAdminRecord:
        async with self._lock:
            if self._state.local_admin is None:
                raise LocalAdminNotFoundError
            now = self._clock()
            previous = self._state.local_admin
            record = LocalAdminRecord(
                password_hash=command.password_hash,
                must_change_password=False,
                # 带上原记录的链接：改密改的是凭据，不是授权，不能顺手把链接清掉。
                user_id=None if previous is None else previous.user_id,
            )
            self._state.local_admin = record
            # 先撤销全部旧的，再插入新的——顺序反过来会把刚签发的一起撤掉。
            for digest, session in list(self._state.web_sessions.items()):
                if (
                    session.auth_source is IdentitySource.LOCAL_ADMIN
                    and session.revoked_at is None
                ):
                    self._state.web_sessions[digest] = session.model_copy(
                        update={"revoked_at": now}
                    )
            self._state.web_sessions[command.new_session_digest] = WebSession(
                session_digest=command.new_session_digest,
                subject_ref=LOCAL_ADMIN_SUBJECT_REF,
                issued_at=now,
                expires_at=now
                + _dt.timedelta(seconds=command.session_ttl_seconds),
                auth_source=IdentitySource.LOCAL_ADMIN,
                public_origin_digest=command.public_origin_digest,
            )
            return record


class InMemoryWebSessionStore:
    """与其他内存存储共锁的 OAuth state 和浏览器 session 实现。"""

    def __init__(
        self,
        *,
        clock: Clock,
        state: InMemoryPersistenceState | None = None,
        oauth_state_capacity: int = DEFAULT_OAUTH_STATE_CAPACITY,
    ) -> None:
        self._clock = clock
        self._oauth_state_capacity = validate_oauth_state_capacity(
            oauth_state_capacity
        )
        self._state = InMemoryPersistenceState() if state is None else state
        self._lock = self._state.lock

    async def issue_oauth_state(
        self, *, command: IssueOAuthStateCommand
    ) -> OAuthState:
        async with self._lock:
            now = self._clock()
            self._cleanup_oauth_states(now=now)
            return self._issue_oauth_state(command=command, now=now)

    async def issue_oauth_login_state(
        self, *, command: IssueOAuthLoginStateCommand
    ) -> OAuthLoginState:
        async with self._lock:
            now = self._clock()
            self._cleanup_oauth_states(now=now)
            state = self._issue_oauth_state(command=command, now=now)
            self._state.oauth_login_contexts[state.state_digest] = (
                command.return_intent
            )
            return OAuthLoginState(
                **state.model_dump(),
                return_intent=command.return_intent,
            )

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

    async def consume_oauth_login_state(
        self, *, command: ConsumeOAuthLoginStateCommand
    ) -> OAuthLoginState:
        async with self._lock:
            state = self._state.oauth_states.get(command.state_digest)
            now = self._clock()
            if (
                state is None
                or state.consumed_at is not None
                or now >= state.expires_at
            ):
                raise OAuthStateNotFoundError
            return_intent = self._state.oauth_login_contexts.get(
                command.state_digest
            )
            if return_intent is None:
                raise OAuthLoginContextNotFoundError
            consumed = state.model_copy(update={"consumed_at": now})
            self._state.oauth_states[state.state_digest] = consumed
            return OAuthLoginState(
                **consumed.model_dump(),
                return_intent=return_intent,
            )

    def _cleanup_oauth_states(self, *, now: _dt.datetime) -> None:
        stale_digests = tuple(
            digest
            for digest, state in self._state.oauth_states.items()
            if state.consumed_at is not None or now >= state.expires_at
        )
        for digest in stale_digests:
            del self._state.oauth_states[digest]
            self._state.oauth_login_contexts.pop(digest, None)

    def _issue_oauth_state(
        self,
        *,
        command: IssueOAuthStateCommand,
        now: _dt.datetime,
    ) -> OAuthState:
        if command.state_digest in self._state.oauth_states:
            raise WebSessionConflictError
        if len(self._state.oauth_states) >= self._oauth_state_capacity:
            raise OAuthStateCapacityError
        state = OAuthState(
            state_digest=command.state_digest,
            issued_at=now,
            expires_at=now + _dt.timedelta(seconds=command.ttl_seconds),
        )
        self._state.oauth_states[state.state_digest] = state
        return state

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
                auth_source=command.auth_source,
                public_origin_digest=command.public_origin_digest,
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
                or session.public_origin_digest != lookup.public_origin_digest
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

    def _create_task_locked(
        self, *, submission: TaskSubmission, digest: str
    ) -> TaskRecord:
        envelope = submission.envelope
        context = submission.context
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
        self._by_key[
            (context.tenant_id, context.environment_id, envelope.idempotency_key)
        ] = record.task_id
        return record

    def _existing_task_for_submission_locked(
        self, *, submission: TaskSubmission, digest: str
    ) -> TaskRecord | None:
        envelope = submission.envelope
        context = submission.context
        scope = (context.tenant_id, context.environment_id, envelope.idempotency_key)
        existing_id = self._by_key.get(scope)
        if existing_id is None:
            return None
        existing = self._records[existing_id]
        if existing.request_digest != digest:
            raise IdempotencyConflictError(
                "idempotency key reused for a different request"
            )
        return existing

    def _clarification_parent_is_consumed_locked(
        self, *, clarification_parent_id: str
    ) -> bool:
        return any(
            submission.clarification_parent_task_id == clarification_parent_id
            for submission in self._state.submissions.values()
        )

    async def create_task(self, *, submission: TaskSubmission) -> TaskRecord:
        envelope = submission.envelope
        context = submission.context
        # 先校验上下文一致性，再谈幂等：不一致时连"属于哪个作用域"都不成立。
        if not context_matches_envelope(envelope, context):
            raise ContextMismatchError("envelope and context disagree on execution context")
        reject_clarification_parent_on_plain_create(submission)
        digest = request_dedup_digest(
            envelope,
            context,
            clarification_parent_task_id=submission.clarification_parent_task_id,
        )
        async with self._lock:
            existing = self._existing_task_for_submission_locked(
                submission=submission, digest=digest
            )
            if existing is not None:
                return existing
            return self._create_task_locked(submission=submission, digest=digest)

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
        context = submission.context
        if not context_matches_envelope(envelope, context):
            raise ContextMismatchError("envelope and context disagree on execution context")
        digest = request_dedup_digest(
            envelope,
            context,
            clarification_parent_task_id=clarification_parent_id,
        )
        async with self._lock:
            existing = self._existing_task_for_submission_locked(
                submission=submission, digest=digest
            )
            if existing is not None:
                return existing
            parent = self._records.get(clarification_parent_id)
            if parent is None or not clarification_parent_is_usable(
                parent=parent,
                submission=submission,
                authenticated_channel_owner=authenticated_channel_owner,
            ):
                raise TaskNotFoundError(task_id=clarification_parent_id)
            if self._clarification_parent_is_consumed_locked(
                clarification_parent_id=clarification_parent_id
            ):
                raise TaskNotFoundError(task_id=clarification_parent_id)
            return self._create_task_locked(submission=submission, digest=digest)

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

"""单进程 TaskStore 实现（DEVELOPMENT_PLAN §7 M2 明文要求置于 persistence/）。

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
"""

import datetime as _dt
import uuid
from typing import Final, TypeVar

from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ApprovalRequest,
    GrantRejection,
    LeaseGrant,
    RetryDecision,
    StepAttemptDecision,
    StepCommitRejection,
    TaskAttemptRejection,
    TaskLookup,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TraceEvent,
    TransitionResult,
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

_EntryT = TypeVar("_EntryT")

IS_FAKE: Final[bool] = True
"""供 tests/security/test_fake_isolation.py 断言；生产模块不得导入本模块。"""


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

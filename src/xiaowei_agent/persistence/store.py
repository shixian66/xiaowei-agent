"""TaskStore 的交互形状。

**本形状被 M3/M4 共用**：若 PostgreSQL adapter 在 M4 被迫改变它，正确做法是回到
M2/M3 修正契约和测试，而不是在 adapter 内加兼容补丁（DEVELOPMENT_PLAN §7 M4）。

四条形状决定及理由：

1. ``transition`` 返回 :class:`TransitionResult` 而非抛异常。CAS 失败是正常并发
   结果，不是异常路径；返回 ``winner`` 强制调用方面对"别人赢了"。用异常则极易
   ``except: pass`` 后继续用本地旧对象——正是 ARCHITECTURE §7.3 明令禁止的。
2. ``fencing_token`` 的规则是**闭合**的，不是"可选校验"：有 live lease 必须带正确
   token；无 live lease 不得带 token。留一个"不传即跳过"的口子等于没有 fencing。
3. token 在**取得**租约或安排任务重试时递增、续租时不变，因此旧持有者的 token
   严格小于当前 token，被抢占或已交接的 worker 拿旧 token 写入必然被拒。
4. 时钟经 :data:`Clock` 注入。租约过期是本模块唯一与时间相关的语义，注入时钟才能
   不 sleep 地测试过期与抢占。
"""

import datetime as _dt
from collections.abc import Callable
from typing import Protocol, Self, TypeAlias

from pydantic import Field, model_validator

from xiaowei_agent.contracts import (
    ApprovalRequest,
    AttemptIntent,
    AwareDatetime,
    Contract,
    LeaseGrant,
    PipelineStage,
    RequestContext,
    RequestEnvelope,
    RetryDecision,
    RetryReason,
    StageOutcome,
    StrictInt,
    StrictStr,
    TaskAttemptRejection,
    TaskLookup,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TraceEvent,
    TraceId,
    TransitionResult,
    content_digest,
)
from xiaowei_agent.persistence.rows import dump_contract
from xiaowei_agent.planning import canonical_json

Clock: TypeAlias = Callable[[], _dt.datetime]


class TaskIdCarryingError(Exception):
    """携带 ``task_id`` 但**不把它放进 ``str(exc)``** 的错误基类。

    ``raise TaskNotFoundError(task_id)`` 会让 task_id 成为异常的唯一参数，于是它
    出现在 ``str(exc)``、``repr(exc)`` 与 traceback 里——与 f-string 回显是同一条
    缺陷，只是没有插值语法，所以只扫字符串拼接的检测器看不到它。

    诊断值放在结构化属性上：需要的调用方显式读 ``exc.task_id``，而默认的错误
    文本恒为常量，不会被日志或错误响应顺手带出去。
    """

    def __init__(self, message: str, *, task_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id


class TaskNotFoundError(TaskIdCarryingError, LookupError):
    """任务不存在。"""

    def __init__(self, *, task_id: str) -> None:
        super().__init__("task not found", task_id=task_id)


class IdempotencyConflictError(RuntimeError):
    """同一幂等键被用于**语义**不同的请求。"""


class ContextMismatchError(RuntimeError):
    """``RequestEnvelope`` 与 ``RequestContext`` 的执行上下文不一致。

    Gateway 从信封解析出上下文，两者不一致意味着解析环节出错或被绕过；此时继续
    创建任务会让任务记在一个未经解析确认的租户/环境下。
    """


class UnscopedAuditEventError(ValueError):
    """审计事件没有 ``task_id``，无处归档。

    ``TraceEvent.task_id`` 是 ``StrictStr | None``：请求在任务被创建之前就失败时
    （信封校验、租户解析），事件确实没有任务可挂。而 ``task_audit_events`` 的
    ``task_id`` 是 ``NOT NULL``——**两边的可空性天生不一致**，落差必须在入口显式
    处理，不能让它变成驱动层的 IntegrityError：那时错误指向连接层，看不出是调用方
    交来了一条不属于任何任务的事件。

    这类事件不是"丢弃就行"，只是不归 TaskStore 管——它们的去处是 ``TraceSink``。
    """


def request_dedup_digest(envelope: RequestEnvelope, context: RequestContext) -> str:
    """幂等去重摘要：只覆盖**语义**字段。

    刻意**排除** ``request_id``——同一 ``idempotency_key`` 的正常重试会带一个新的
    ``request_id``，把它算进摘要会让合法重试被误判为"同键不同请求"。同理排除
    trace_id 与任何时间戳。保留的是"这两次请求是不是在问同一件事"的判据。
    """
    payload = {
        "tenant_id": context.tenant_id,
        "environment_id": context.environment_id,
        "actor": context.actor,
        "channel": envelope.channel.value,
        "text": envelope.text,
        "idempotency_key": envelope.idempotency_key,
    }
    return content_digest(canonical_json(payload).decode("utf-8"))


def idempotency_scope_digest(
    *, tenant_id: str, environment_id: str, idempotency_key: str
) -> str:
    """定长幂等作用域 checksum；命中后仍必须回查三项明文。"""
    payload = {
        "tenant_id": tenant_id,
        "environment_id": environment_id,
        "idempotency_key": idempotency_key,
    }
    return content_digest(canonical_json(payload).decode("utf-8"))


def submission_digest(submission: TaskSubmission) -> str:
    """完整提交事实的一致性 checksum；不作为抗篡改证明。"""
    payload = {
        "envelope": dump_contract(submission.envelope),
        "context": dump_contract(submission.context),
        "as_of": submission.as_of.isoformat(),
    }
    return content_digest(canonical_json(payload).decode("utf-8"))


def submission_matches_record(
    record: TaskRecord, submission: TaskSubmission, *, stored_digest: str
) -> bool:
    """提交行是否仍与创建时任务事实一致；摘要只防代码缺陷，不作安全声明。"""
    from xiaowei_agent.persistence.decisions import context_matches_envelope

    return (
        stored_digest == submission_digest(submission)
        and record.request_digest
        == request_dedup_digest(submission.envelope, submission.context)
        and context_matches_envelope(submission.envelope, submission.context)
        and record.tenant_id == submission.context.tenant_id
        and record.environment_id == submission.context.environment_id
        and record.actor == submission.context.actor
    )


def validate_task_failure_limit(value: int) -> int:
    """校验存储实现共享的失败预算构造参数，显式拒绝 ``bool``。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("task_failure_limit must be an integer")
    if value <= 0:
        raise ValueError("task_failure_limit must be positive")
    return value


def attempt_terminal_event(
    *, command: "TaskAttemptCommand", winner: TaskRecord, now: _dt.datetime
) -> TraceEvent:
    """构造 grant 之前由存储层原子终态化所需的唯一审计形状。"""
    import uuid

    return TraceEvent(
        event_id=str(uuid.uuid4()),
        trace_id=command.trace_id,
        task_id=winner.task_id,
        stage=PipelineStage.LIFECYCLE,
        outcome=StageOutcome.FAILED,
        occurred_at=now,
        capability_id=None,
        step_id=None,
        policy_revision=None,
        attempt_number=winner.attempt_number,
        error=None,
        detail={},
    )


class DispatchQuery(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    limit: StrictInt = Field(gt=0)


class TaskAttemptCommand(Contract):
    task_id: StrictStr
    intent: AttemptIntent
    owner: StrictStr
    ttl_seconds: StrictInt = Field(gt=0)
    trace_id: TraceId


class TaskAttemptGrant(Contract):
    lease: LeaseGrant
    attempt_number: StrictInt = Field(gt=0)

    @property
    def task_id(self) -> str:
        return self.lease.task_id

    @property
    def fencing_token(self) -> int:
        return self.lease.fencing_token


_ATTEMPT_TERMINAL_REJECTIONS = {
    TaskAttemptRejection.RETRY_EXHAUSTED,
    TaskAttemptRejection.SUBMISSION_INVARIANT_VIOLATION,
}


class TaskAttemptResult(Contract):
    applied: bool
    winner: TaskRecord
    grant: TaskAttemptGrant | None = None
    submission: TaskSubmission | None = None
    rejection: TaskAttemptRejection | None = None
    committed_audit_events: tuple[TraceEvent, ...] = ()

    @model_validator(mode="after")
    def _result_is_self_consistent(self) -> Self:
        if self.applied != (self.grant is not None):
            raise ValueError("applied must agree with grant presence")
        if self.applied != (self.submission is not None):
            raise ValueError("applied must agree with submission presence")
        if self.applied == (self.rejection is not None):
            raise ValueError("applied and rejection must be mutually exclusive")
        rejection = self.rejection
        should_have_audit = rejection in _ATTEMPT_TERMINAL_REJECTIONS
        if should_have_audit != bool(self.committed_audit_events):
            raise ValueError("committed audit presence does not match the decision")
        if should_have_audit and (
            self.winner.status is not TaskStatus.FAILED
            or rejection is None
            or self.winner.terminal_reason != rejection.value
        ):
            raise ValueError("terminal rejection must return its newly failed winner")
        for event in self.committed_audit_events:
            if event.task_id != self.winner.task_id:
                raise ValueError("committed audit belongs to a different task")
            if event.attempt_number != self.winner.attempt_number:
                raise ValueError("committed audit belongs to a different attempt")
        if self.applied:
            if self.grant is None or self.submission is None:
                raise ValueError("successful result is incomplete")
            lease = self.grant.lease
            if (
                lease.task_id != self.winner.task_id
                or lease.owner != self.winner.lease_owner
                or lease.fencing_token != self.winner.fencing_token
                or lease.expires_at != self.winner.lease_expires_at
                or self.grant.attempt_number != self.winner.attempt_number
            ):
                raise ValueError("grant does not describe the winner lease")
            from xiaowei_agent.persistence.decisions import context_matches_envelope

            submission = self.submission
            if not context_matches_envelope(submission.envelope, submission.context):
                raise ValueError("submission context does not match its envelope")
            if (
                self.winner.tenant_id != submission.context.tenant_id
                or self.winner.environment_id != submission.context.environment_id
                or self.winner.actor != submission.context.actor
            ):
                raise ValueError("submission does not belong to the winner")
        return self


class RetryCommand(Contract):
    grant: TaskAttemptGrant
    next_attempt_at: AwareDatetime
    reason: RetryReason
    audit_events: tuple[TraceEvent, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _audit_belongs_to_the_attempt(self) -> Self:
        for event in self.audit_events:
            if event.task_id != self.grant.task_id:
                raise ValueError("retry audit belongs to a different task")
            if event.attempt_number != self.grant.attempt_number:
                raise ValueError("retry audit belongs to a different attempt")
        return self


class RetryResult(Contract):
    decision: RetryDecision
    winner: TaskRecord


def retry_command_digest(command: RetryCommand) -> str:
    """重试命令的一致性 checksum；不作为抗篡改证明。"""
    payload = {
        "next_attempt_at": command.next_attempt_at.isoformat(),
        "reason": command.reason.value,
        "attempt_number": command.grant.attempt_number,
    }
    return content_digest(canonical_json(payload).decode("utf-8"))


class TransitionCommand(Contract):
    """``transition`` 的入参 DTO。

    Protocol 的类型标注在运行时不拦任何东西：``expected_version=False`` 会匹配
    版本 ``0``、``fencing_token=True`` 会匹配 token ``1``、``to_status="planning"``
    会被转成枚举。把入参构造成严格契约，这些隐式转换在入口就失败。
    """

    task_id: StrictStr
    expected_version: StrictInt = Field(ge=0)
    to_status: TaskStatus
    fencing_token: StrictInt | None = Field(default=None, gt=0)
    terminal_reason: StrictStr | None = None


class StaleLeaseQuery(Contract):
    """``list_stale_leases`` 的入参 DTO。

    与另外两个命令 DTO 同一理由：``limit=True`` 在运行时会被当作 ``1``，
    ``limit="10"`` 会被当作 10。只有一个参数也照样构造 DTO——"参数少所以不会写错"
    正是这类隐式转换能长期潜伏的原因。
    """

    limit: StrictInt = Field(gt=0)


class LeaseCommand(Contract):
    """``acquire_lease`` / ``renew_lease`` 的入参 DTO。"""

    task_id: StrictStr
    owner: StrictStr
    ttl_seconds: StrictInt = Field(gt=0)
    fencing_token: StrictInt | None = Field(default=None, gt=0)


class TaskStore(Protocol):
    async def create_task(self, *, submission: TaskSubmission) -> TaskRecord:
        """按 ``(tenant_id, environment_id, idempotency_key)`` 幂等创建。

        :raises ContextMismatchError: 信封与执行上下文的 tenant/actor/environment 不一致。
        :raises IdempotencyConflictError: 同一作用域内同键但**语义**不同的请求。
        """

    async def get(self, *, lookup: TaskLookup) -> TaskRecord:
        """:raises TaskNotFoundError: 任务不存在或不属于指定作用域。"""

    async def transition(
        self,
        *,
        task_id: str,
        expected_version: int,
        to_status: TaskStatus,
        fencing_token: int | None = None,
        terminal_reason: str | None = None,
    ) -> TransitionResult:
        """CAS 状态迁移。**永远返回存储层 winner**，调用方必须采纳它。"""

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        """取得租约并分配单调递增 fencing token。

        已被他人持有、或任务已终态时返回 ``None``——终态任务不应再被任何 worker
        领走。
        """

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> LeaseGrant | None:
        """续租保持同一 token；非持有者、token 陈旧或租约已过期时返回 ``None``。"""

    async def list_stale_leases(self, *, limit: int) -> tuple[TaskRecord, ...]:
        """列出曾被租出、租约已过期、且未处于终态的任务。

        按 ``(lease_expires_at, task_id)`` 稳定排序，最多返回 ``limit`` 条。

        **只回答"哪些任务可能需要被接管"**：不 claim、不调度、不判断审批是否应当
        恢复、不改变任何状态。接管仍走 ``acquire_lease()``，并发 winner 仍由存储层
        裁决——这条方法返回的记录随时可能已被别人接管，调用方不得据此假定自己拿到
        了任何独占权。

        这条边界是刻意的：把"发现"和"接管"合成一个方法，会让 TaskStore 长出调度
        能力，而调度属 M5 的 Worker。
        """

    async def record_approval(self, *, request: ApprovalRequest) -> int:
        """append-only 写入审批记录，返回本任务内分配到的 ``seq``（从 1 开始）。

        **为什么写方法有返回值**：``seq`` 是"这条记录在本任务的审批历史里排第几"。
        不返回它，两个实现就可以对序号给出完全不同的答案而没有任何断言能发现——
        M4 之前正是如此：PostgreSQL 按 ``(task_id, seq)`` 编号，内存实现只往一个
        扁平列表里 append，连 seq 的概念都没有。返回值让同一批用例能在两个实现上
        逐条检查同一件事。

        这不是"审批读回"（M4 §8.4 拍板不加读回 Protocol 方法）：它是本次写入自己的
        回执，不查询任何既存记录，也不构成消费路径——消费路径仍归 M8。
        """

    async def record_audit_event(self, *, event: TraceEvent) -> int:
        """append-only 写入审计事件，返回本任务内分配到的 ``seq``（从 1 开始）。

        **证据表替代不了审计**：证据回答"看到了什么"，审计回答"系统做了什么、准入
        判成了什么"。一次被策略拒绝的调用不产生任何证据，但必须留下审计。

        载荷是 M2 的 ``TraceEvent``，其 ``detail`` 在契约层已做键值双向脱敏并限长，
        因此这里不再脱敏一次——再写一份就等于给脱敏规则开了第二个可能漂移的副本。

        :raises UnscopedAuditEventError: ``event.task_id`` 为 ``None``。
        """

    async def list_dispatchable_tasks(
        self, *, query: DispatchQuery
    ) -> tuple[TaskRecord, ...]:
        """列出普通 Worker 可竞争的候选；不领取、不改变状态。"""

    async def begin_task_attempt(
        self, *, command: TaskAttemptCommand
    ) -> TaskAttemptResult:
        """原子竞争一次任务尝试，并随成功 grant 返回不可变提交事实。"""

    async def schedule_retry(self, *, command: RetryCommand) -> RetryResult:
        """原子结束当前尝试并安排下一次可领取时间。"""

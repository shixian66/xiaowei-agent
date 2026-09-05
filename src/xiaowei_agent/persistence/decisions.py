"""TaskStore 的判定规则：**纯函数，两个实现共用**。

M4 引入 ``PostgresTaskStore`` 时，最危险的失败不是"实现写错了"，而是"实现写得**不
一样**"——两套判定各自看起来合理，但拒绝顺序差一位、fencing 锚点差一个词，于是同一
序列在内存里被拒、在库里被接受。这类分叉不会被任何单实现的用例发现，因为每个实现
都通过自己那份断言。

因此判定从存储里搬出来，只留一份：**决定"能不能"的是本模块，决定"怎么存"的才是
实现。** 本模块因此有一条硬约束——

    无 I/O、无 SQLAlchemy、无 async、无全局可变状态。

时间由调用方以 ``now`` 传入，不在此处读时钟：读时钟就是 I/O，会让"同一输入必得同一
输出"不再成立，而这正是两个实现可以被同一份断言约束的前提。

这条取向有代价，且必须说清：**纯函数里的 bug 会让两个实现同时通过**。共享判定消除
的是"分叉"，不是"判错"。判错由 T8 的变异反证承重——撤掉任一承重规则，现有 CAS /
fencing / terminal 用例必须转红。

---

**拒绝顺序是有语义的**：终态保护 → 租约/fencing → 版本 → 迁移合法性。

终态放最前，使"违反终态保护"永远以 ``TERMINAL_PROTECTED`` 报出，不会被版本不匹配
掩盖成一个看起来正常的并发结果；租约放在版本之前，是因为"拿着陈旧 token 的 worker"
比"版本落后"更具体，审计需要看到前者。

**fencing 规则锚定在"任务是否曾被租出"，而不是"租约此刻是否 live"**：后者留了一个
致命缺口——租约过期后，stale worker 只要**不带 token** 就能写入。闭合规则是：

===================  ==================  ==========================
任务曾被租出          传入 token          结果
===================  ==================  ==========================
否                    未传               放行（created → planning 发生在取租约前）
否                    传了               ``LEASE_NOT_HELD``（持有不存在的租约）
是，租约 live         与当前 token 相同   放行
是，租约 live         与当前 token 不同   ``STALE_FENCING_TOKEN``
是，租约 live         未传               ``LEASE_NOT_HELD``
是，租约已过期        任意                ``LEASE_NOT_HELD``（必须重新 acquire）
===================  ==================  ==========================
"""

import datetime as _dt

from xiaowei_agent.contracts import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    AttemptIntent,
    GrantRejection,
    RequestContext,
    RequestEnvelope,
    TaskAttemptRejection,
    TaskRecord,
    TaskStatus,
    TransitionRejection,
)
from xiaowei_agent.persistence.store import (
    DispatchQuery,
    TaskAttemptGrant,
    TransitionCommand,
)

__all__ = [
    "apply_transition",
    "attempt_statuses",
    "classify_task_attempt",
    "classify_transition",
    "context_matches_envelope",
    "dispatch_sort_key",
    "grant_is_current",
    "is_dispatchable",
    "is_stale_lease",
    "lease_is_live",
    "may_acquire_lease",
    "may_renew_lease",
    "stale_lease_sort_key",
]

_DISPATCH_STATUSES = frozenset(
    {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}
)
_APPROVAL_RESUME_STATUSES = frozenset({TaskStatus.AWAITING_APPROVAL})


def attempt_statuses(intent: AttemptIntent) -> frozenset[TaskStatus]:
    """返回某种领取意图唯一允许的状态集合。"""
    if intent is AttemptIntent.DISPATCH:
        return _DISPATCH_STATUSES
    return _APPROVAL_RESUME_STATUSES


def is_dispatchable(
    record: TaskRecord, query: DispatchQuery, *, now: _dt.datetime
) -> bool:
    """任务是否是指定作用域内此刻可供普通 Worker 竞争的候选。"""
    return (
        record.tenant_id == query.tenant_id
        and record.environment_id == query.environment_id
        and record.status in _DISPATCH_STATUSES
        and (record.next_attempt_at is None or record.next_attempt_at <= now)
        and not lease_is_live(record, now=now)
    )


def dispatch_sort_key(record: TaskRecord) -> tuple[int, str]:
    """稳定的近似公平次序；sequence 相同仍由主键决胜。"""
    return (record.created_seq, record.task_id)


def classify_task_attempt(
    record: TaskRecord,
    *,
    intent: AttemptIntent,
    now: _dt.datetime,
    task_failure_limit: int,
) -> TaskAttemptRejection | None:
    """grant 之前的共享拒绝顺序；submission 完整性由存储事务另行判定。"""
    if record.status in TERMINAL_STATUSES:
        return TaskAttemptRejection.TERMINAL_PROTECTED
    if record.status not in attempt_statuses(intent):
        return TaskAttemptRejection.NOT_DISPATCHABLE
    if record.next_attempt_at is not None and record.next_attempt_at > now:
        return TaskAttemptRejection.RETRY_NOT_DUE
    if lease_is_live(record, now=now):
        return TaskAttemptRejection.LIVE_LEASE
    if record.task_failure_count >= task_failure_limit:
        return TaskAttemptRejection.RETRY_EXHAUSTED
    return None


def grant_is_current(
    record: TaskRecord,
    grant: TaskAttemptGrant,
    *,
    now: _dt.datetime,
    allowed_statuses: frozenset[TaskStatus],
) -> GrantRejection | None:
    """``None`` 表示 grant 此刻仍是该任务唯一合法的 fenced 写入者。"""
    if record.status in TERMINAL_STATUSES:
        return GrantRejection.TERMINAL_PROTECTED
    if record.status not in allowed_statuses:
        return GrantRejection.STATUS_NOT_ALLOWED
    if not lease_is_live(record, now=now):
        return GrantRejection.LEASE_NOT_HELD
    if (
        record.lease_owner != grant.lease.owner
        or record.fencing_token != grant.fencing_token
        or record.attempt_number != grant.attempt_number
    ):
        return GrantRejection.STALE_FENCING
    return None


def lease_is_live(record: TaskRecord, *, now: _dt.datetime) -> bool:
    """租约此刻是否有效。

    三个租约字段必须同时有值：``TaskRecord`` 的校验器保证它们同置同清，这里不再
    重复该不变量，只在读取侧保持一致的窄条件。
    """
    return (
        record.lease_owner is not None
        and record.lease_expires_at is not None
        and record.lease_expires_at > now
    )


def _check_fencing(
    record: TaskRecord, fencing_token: int | None, *, now: _dt.datetime
) -> TransitionRejection | None:
    """按"任务是否曾被租出"判定，而非"租约此刻是否 live"。

    以 live 为锚点会留下缺口：租约过期后 stale worker 只要不带 token 就能写入。
    """
    ever_leased = record.fencing_token is not None
    if not ever_leased:
        # 从未租出：不带 token 放行（created → planning）；带 token 即违规。
        return None if fencing_token is None else TransitionRejection.LEASE_NOT_HELD
    if not lease_is_live(record, now=now):
        # 曾被租出但租约已过期：必须重新 acquire，无论是否带 token。
        return TransitionRejection.LEASE_NOT_HELD
    if fencing_token is None:
        return TransitionRejection.LEASE_NOT_HELD
    if fencing_token != record.fencing_token:
        return TransitionRejection.STALE_FENCING_TOKEN
    return None


def classify_transition(
    record: TaskRecord, command: TransitionCommand, *, now: _dt.datetime
) -> TransitionRejection | None:
    """迁移是否被拒；``None`` 表示可以应用。

    ``record`` 必须是**当前存储中的值**，``command.expected_version`` 必须是**调用方
    传入的值**。两者若同源，版本检查就退化成恒真的空洞检查——CAS 永远成功，并发写
    全部提交。PostgreSQL 实现里这条尤其容易踩：事务内 ``SELECT ... FOR UPDATE`` 拿到
    的行既是 ``record`` 又"看起来像"期望版本。
    """
    if record.status in TERMINAL_STATUSES:
        return TransitionRejection.TERMINAL_PROTECTED
    rejection = _check_fencing(record, command.fencing_token, now=now)
    if rejection is not None:
        return rejection
    if record.version != command.expected_version:
        return TransitionRejection.VERSION_MISMATCH
    if command.to_status not in ALLOWED_TRANSITIONS[record.status]:
        return TransitionRejection.ILLEGAL_TRANSITION
    return None


def apply_transition(record: TaskRecord, command: TransitionCommand) -> TaskRecord:
    """迁移通过后的新记录。**只在 ``classify_transition`` 返回 ``None`` 时调用。**

    ``terminal_reason`` **无条件写入，包括写 ``None``**。这不是疏忽：先失败置上原因、
    再重试转为非终态时，若写成"有值才更新"，旧原因会残留，调用方看到的是一条状态与
    原因互相矛盾的记录。PostgreSQL 实现必须同样无条件 ``SET``，包括 ``SET ... = NULL``。
    """
    return record.model_copy(
        update={
            "status": command.to_status,
            "version": record.version + 1,
            "terminal_reason": command.terminal_reason,
        }
    )


def may_acquire_lease(record: TaskRecord, *, owner: str, now: _dt.datetime) -> bool:
    """能否领取租约。

    已终态的任务不应再被任何 worker 领走；租约仍 live 且属于他人时不得抢占——过期
    才是抢占的唯一入口，否则"租约"不再提供任何互斥保证。同一 owner 可以重入。
    """
    if record.status in TERMINAL_STATUSES:
        return False
    return not (lease_is_live(record, now=now) and record.lease_owner != owner)


def may_renew_lease(
    record: TaskRecord, *, owner: str, fencing_token: int, now: _dt.datetime
) -> bool:
    """能否续租。

    过期后必须重新 ``acquire``，不能续：否则一个已被他人抢占的租约可以被原主复活，
    于是同一任务出现两个自认持有者。owner 与 token 都必须匹配——只比 owner 会让
    同名 worker 的旧进程续上新进程的租约。
    """
    if not lease_is_live(record, now=now):
        return False
    return record.lease_owner == owner and record.fencing_token == fencing_token


def is_stale_lease(record: TaskRecord, *, now: _dt.datetime) -> bool:
    """任务是否**可能**需要被接管：曾被租出、租约已过期、且未处于终态。

    三个条件缺一不可，且每一条挡的是不同的错误：

    - **曾被租出**（``fencing_token is not None``）——从未租出的任务没有"接管"可言，
      它还没开始。锚点与 fencing 规则一致，都不是"租约此刻是否 live"。
    - **租约已过期**——仍在有效期内的任务有活着的持有者，列出它等于邀请抢占。
    - **未终态**——已结束的任务不需要被恢复。去掉这一条，每个正常结束的任务都会
      被永久列为"可恢复"，因为它的租约字段并不清空。

    这个判定只回答"可能需要被接管"。它**不**回答该不该恢复、由谁恢复、恢复后做
    什么——那些是 Worker 的职责，属 M5 及以后。
    """
    return (
        record.fencing_token is not None
        and not lease_is_live(record, now=now)
        and record.status not in TERMINAL_STATUSES
    )


def stale_lease_sort_key(record: TaskRecord) -> tuple[_dt.datetime, str]:
    """``(lease_expires_at, task_id)``：过期最久的排最前，同刻按 id 决胜。

    排序必须**稳定且全序**，否则两次调用可能返回不同的前 N 条，而调用方会以为
    自己看到的是"最该处理的那些"。只按时间排不够——同一毫秒过期的多条租约顺序
    不确定；``task_id`` 是主键，用它决胜保证全序。

    只对 ``is_stale_lease`` 为真的记录调用。传入一条从未租出的记录会抛错，而不是
    返回一个编造的时间戳——后者会让它悄悄排到最前面，看起来像"最该处理的任务"。
    """
    expires_at = record.lease_expires_at
    if expires_at is None:
        raise ValueError("stale_lease_sort_key requires a record that was ever leased")
    return (expires_at, record.task_id)


def context_matches_envelope(envelope: RequestEnvelope, context: RequestContext) -> bool:
    """信封与执行上下文是否描述同一个作用域。

    不一致时连"属于哪个租户/环境的幂等作用域"都不成立，因此这条检查必须早于幂等
    查找。``environment_id`` 在信封里可省略，省略即不约束；一旦给出就必须相等。
    """
    return (
        envelope.tenant_id == context.tenant_id
        and envelope.actor == context.actor
        and (
            envelope.environment_id is None
            or envelope.environment_id == context.environment_id
        )
    )

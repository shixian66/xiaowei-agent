"""TaskStore 的交互形状。

**本形状被 M3/M4 共用**：若 PostgreSQL adapter 在 M4 被迫改变它，正确做法是回到
M2/M3 修正契约和测试，而不是在 adapter 内加兼容补丁（DEVELOPMENT_PLAN §7 M4）。

四条形状决定及理由：

1. ``transition`` 返回 :class:`TransitionResult` 而非抛异常。CAS 失败是正常并发
   结果，不是异常路径；返回 ``winner`` 强制调用方面对"别人赢了"。用异常则极易
   ``except: pass`` 后继续用本地旧对象——正是 ARCHITECTURE §7.3 明令禁止的。
2. ``fencing_token`` 的规则是**闭合**的，不是"可选校验"：有 live lease 必须带正确
   token；无 live lease 不得带 token。留一个"不传即跳过"的口子等于没有 fencing。
3. token 在**取得**租约时递增、续租时不变，因此"旧持有者的 token < 当前 token"
   恒成立，被抢占的 worker 拿旧 token 写入必然被拒。
4. 时钟经 :data:`Clock` 注入。租约过期是本模块唯一与时间相关的语义，注入时钟才能
   不 sleep 地测试过期与抢占。
"""

import datetime as _dt
from collections.abc import Callable
from typing import Protocol, TypeAlias

from pydantic import Field

from xiaowei_agent.contracts import (
    ApprovalRequest,
    Contract,
    LeaseGrant,
    RequestContext,
    RequestEnvelope,
    StrictInt,
    StrictStr,
    TaskRecord,
    TaskStatus,
    TransitionResult,
    content_digest,
)
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


class LeaseCommand(Contract):
    """``acquire_lease`` / ``renew_lease`` 的入参 DTO。"""

    task_id: StrictStr
    owner: StrictStr
    ttl_seconds: StrictInt = Field(gt=0)
    fencing_token: StrictInt | None = Field(default=None, gt=0)


class TaskStore(Protocol):
    async def create_task(
        self, *, envelope: RequestEnvelope, context: RequestContext
    ) -> TaskRecord:
        """按 ``(tenant_id, environment_id, idempotency_key)`` 幂等创建。

        :raises ContextMismatchError: 信封与执行上下文的 tenant/actor/environment 不一致。
        :raises IdempotencyConflictError: 同一作用域内同键但**语义**不同的请求。
        """

    async def get(self, task_id: str) -> TaskRecord:
        """:raises TaskNotFoundError: 任务不存在。"""

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

    async def record_approval(self, *, request: ApprovalRequest) -> None: ...

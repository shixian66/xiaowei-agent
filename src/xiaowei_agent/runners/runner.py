"""Runner 契约。签名与 ARCHITECTURE §5.6 一致。"""

from typing import Protocol

from xiaowei_agent.contracts import (
    ApprovalRequest,
    ExecutionPlan,
    ExternalInput,
    RequestContext,
    ResolvedTarget,
    TaskOutcome,
)
from xiaowei_agent.persistence.store import TaskAttemptGrant


class WorkflowPaused(Exception):  # noqa: N818 —— 见下方 docstring：它不是错误
    """任务在副作用步骤前持久化暂停。**这是 WorkflowRunner 契约的一部分。**

    刻意**不**叫 ``...Error``（ruff N818 已就此单点豁免）：暂停不是失败。任务处在
    一个完全正常、可恢复的状态；把它命名成错误会诱导调用方按失败路径处理，
    例如记为一次故障、或直接置终态。

    ``start()`` / ``resume()`` 的返回类型是 ``TaskOutcome``，而 ``TaskOutcome`` 要求
    终态；``awaiting_approval`` 是非终态，因此"暂停"在返回值里**不可表达**。把它
    写进契约本身（而不是让调用方自己猜），Runtime 才能捕获后返回可审计的 pending
    payload。

    ``task_id`` / ``step_id`` / ``approval_ref`` 放结构化属性，**不进 ``str(exc)``**
    ——沿用 ``TaskIdCarryingError`` 的写法：诊断值需要时显式读属性，默认错误文本
    恒为常量，不会被日志或错误响应顺手带出去。
    """

    def __init__(self, *, task_id: str, step_id: str, approval_ref: str) -> None:
        super().__init__("workflow paused awaiting approval")
        self.task_id = task_id
        self.step_id = step_id
        self.approval_ref = approval_ref


class WorkflowRunner(Protocol):
    """Runner 契约。**签名包含活输入，这是 M3 对 M2 契约的修正。**

    M2 把接口写成 ``start(task_id)`` / ``resume(task_id, external_input)``，隐含
    "Runner 只要 task_id 就能推进任务"。真正实现 ARCHITECTURE §5.6 赋予 Runner 的
    职责后，这个前提站不住：

    - **恢复时的漂移检测需要一个活的对照物。** ``resume`` 要判断"暂停期间 policy
      或目标是否变了"，就必须拿到调用方**当下重新解析**出的 ``target`` 与
      ``context.policy_revision``，再与 ``PlanStore`` 里存的那份比对。若两边都从存储
      读，比的是同一个值，检查恒真——安全检查会静默变成一句空话。这两项按定义
      不可持久化：它们的意义就是"现在的值"。
    - **Runner 不拥有领域安全规则**（§5.6），因此它不能自己去重解析目标；重解析是
      Resolver/PlanCompiler 的职责，结果只能由调用方传入。

    把它们留在签名之外，唯一的写法就是让调用方绕过 Protocol 去调具体类——M3 一度
    正是这么做的（Runtime 把 runner 标成 ``object`` 加 ``type: ignore``），于是
    Runtime→Runner 这条边在类型层完全失去契约。修 M2 契约比在 M3 打兼容补丁正确。

    ``plan`` / ``target`` 在 ``start`` 是入参而非从 ``PlanStore`` 读：新编译的计划
    此刻尚未落库，``start`` 的第一件事正是把它存进去。``resume`` 同时接收当下重算的
    ``plan`` 并从存储取回**当初那份**：前者只用于漂移检测，验证通过后执行后者。
    M5 再把 ``task_id`` 收窄成调度层签发的 ``TaskAttemptGrant``：Runner 只能验证、
    续租并使用这份执行权，不得自行竞争另一份租约。
    """

    async def start(
        self,
        grant: TaskAttemptGrant,
        *,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
    ) -> TaskOutcome: ...

    async def resume(
        self,
        grant: TaskAttemptGrant,
        external_input: ExternalInput | None = None,
        *,
        plan: ExecutionPlan,
        context: RequestContext,
        target: ResolvedTarget,
        approval: ApprovalRequest | None = None,
    ) -> TaskOutcome: ...

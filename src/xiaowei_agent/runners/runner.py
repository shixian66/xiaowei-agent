"""Runner 契约。签名与 ARCHITECTURE §5.6 一致。"""

from typing import Protocol

from xiaowei_agent.contracts import ExternalInput, TaskOutcome


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
    async def start(self, task_id: str) -> TaskOutcome: ...

    async def resume(
        self, task_id: str, external_input: ExternalInput | None = None
    ) -> TaskOutcome: ...

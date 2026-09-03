"""ApprovalGate：副作用步骤前的执行中断点。

ApprovalGate **不是入口总开关**：只读步骤根本不经过它（ARCHITECTURE §5.7）。
它只在具体副作用步骤边界被 Runner 调用。
"""

import datetime as _dt
from typing import Final, Protocol

from xiaowei_agent.contracts import (
    ApprovalRequest,
    BindingRejection,
    ExecutionPlan,
    PlanStep,
    ResolvedTarget,
)
from xiaowei_agent.governance.binding import BindingError, verify_approval_binding

_APPROVAL_REF_SEPARATOR: Final[str] = ":"


def approval_ref(*, task_id: str, step_id: str) -> str:
    """审批引用的**唯一**拼装点。

    暂停时交给调用方的 ref、恢复时用来核对 ``ExternalInput.approval_ref`` 的 ref、
    以及 gate 通过后返回的 ref 必须逐字节相同——它们本就是同一个标识。分散在几处
    各写一遍 f-string，任何一处改了分隔符，暂停发出的 ref 就再也匹配不上恢复时
    校验的 ref，而这不会有任何测试自然失败：几处各自都"自洽"。
    """
    return f"{task_id}{_APPROVAL_REF_SEPARATOR}{step_id}"


class ApprovalRequiredError(RuntimeError):
    """步骤需要审批而当前没有有效审批。

    ``step_id`` 放结构化属性而不进消息：Runner 需要它来创建审批请求，而拒绝路径
    统一不回显调用方取值。
    """

    def __init__(self, *, step_id: str) -> None:
        super().__init__("side effect step requires an approval")
        self.step_id = step_id


class ApprovalGate(Protocol):
    """审批判定的唯一契约。"""

    def require(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        approval: ApprovalRequest | None,
        now: _dt.datetime,
    ) -> str: ...


class NeverGrantingApprovalGate:
    """M3 的唯一实现：**从不自动批准**。

    这不是 fake——它是一个真实的、策略 fail-closed 的 ApprovalGate。审批主体、
    渠道与有效期要到 ADR-005（M8 前）才定稿，在那之前"自动批准"没有任何合法
    来源，因此拒绝是正确行为而不是占位。

    持有已 GRANTED 的 ``ApprovalRequest`` 时，它调用 M2 的
    ``verify_approval_binding`` 复核绑定，通过才返回 ``approval_ref``——复核用的是
    与恢复路径**同一份**实现，两处不会各写一套。
    """

    def require(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        approval: ApprovalRequest | None,
        now: _dt.datetime,
    ) -> str:
        """校验审批并返回可审计的引用。

        :raises ApprovalRequiredError: 没有提供审批。
        :raises BindingError: 审批过期、未授予、绑定的任一指纹漂移，或它属于
            另一个任务/步骤。
        """
        if approval is None:
            raise ApprovalRequiredError(step_id=step.step_id)
        if approval.task_id != task_id or approval.step_id != step.step_id:
            # 与指纹漂移同一类问题：一张为别处签发的审批被拿来用在这里。
            raise BindingError(
                BindingRejection.PLAN_DRIFT, "approval belongs to another task or step"
            )
        verify_approval_binding(approval=approval, plan=plan, target=target, now=now)
        return approval_ref(task_id=approval.task_id, step_id=approval.step_id)

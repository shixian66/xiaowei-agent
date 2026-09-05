"""尚未形成 ExecutionPlan 时的通用终态投影。"""

from typing import Final

from xiaowei_agent.contracts import RenderPayload, TaskStatus

_PREPLAN_REJECTED: Final[str] = "请求在执行前被拒绝，未调用任何工具。"


def render_preplan_rejection(*, status: TaskStatus) -> RenderPayload:
    """只投影确定性拒绝；没有计划时不得猜测领域事实。"""
    if status is not TaskStatus.REJECTED:
        raise ValueError("generic pre-plan projection requires rejected status")
    return RenderPayload(
        answer=_PREPLAN_REJECTED,
        sections=(),
        next_steps=(),
        status=status,
        refs=(),
    )

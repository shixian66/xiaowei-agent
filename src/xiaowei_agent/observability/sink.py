"""trace / audit 事件出口契约与显式投递闭集。"""

from enum import StrEnum
from typing import Protocol

from xiaowei_agent.contracts import TraceEvent


class Delivery(StrEnum):
    """事件的传输结局；它不是任务事实，因此不进入 ``TraceEvent``。"""

    LOG_ONLY = "log_only"
    LOG_AND_DURABLE = "log_and_durable"
    COMMAND_COMMITTED = "command_committed"
    COMMAND_ROLLED_BACK = "command_rolled_back"
    COMMAND_NOT_CONFIRMED = "command_not_confirmed"


class AuditEventWriter(Protocol):
    """durable sink 所需的最小写端口；不暴露 TaskStore 的其余能力。"""

    async def record_audit_event(self, *, event: TraceEvent) -> int: ...


def validate_delivery(event: TraceEvent, delivery: Delivery) -> None:
    """所有 sink 共用的运行期归属守卫。"""
    if delivery is not Delivery.LOG_ONLY and event.task_id is None:
        raise ValueError("task-scoped delivery requires a task-scoped event")


def delivery_for_write_exception(exc: Exception) -> Delivery:
    """没有显式回滚证明时一律归“无法确认”，不按异常类型猜。"""
    outcome = getattr(exc, "write_outcome", None)
    if getattr(outcome, "value", None) == "rolled_back":
        return Delivery.COMMAND_ROLLED_BACK
    return Delivery.COMMAND_NOT_CONFIRMED


class TraceSink(Protocol):
    async def emit(self, event: TraceEvent, *, delivery: Delivery) -> None: ...

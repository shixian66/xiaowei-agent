"""把 trace 事件写进标准库 logging。

只依赖 ``contracts`` 与标准库 ``logging``：**不引入任何采集后端**。事件的
``detail`` 已在契约层脱敏并限长，因此本模块**不再自己拼一份原文**——那正是绕过
脱敏边界最容易发生的地方。
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from xiaowei_agent.contracts import TraceEvent
from xiaowei_agent.observability.sink import Delivery, validate_delivery

LOGGER_NAME: Final[str] = "xiaowei_agent.trace"

_LOGGER: Final[logging.Logger] = logging.getLogger(LOGGER_NAME)
_WORKER_INSTANCE: ContextVar[str | None] = ContextVar(
    "xiaowei_worker_instance", default=None
)

_DELIVERY_STATE: Final[Mapping[Delivery, str]] = {
    Delivery.LOG_ONLY: "not_applicable",
    Delivery.COMMAND_COMMITTED: "committed",
    Delivery.COMMAND_ROLLED_BACK: "failed",
    Delivery.COMMAND_NOT_CONFIRMED: "not_confirmed",
}


@contextmanager
def worker_log_context(worker_instance: str) -> Iterator[None]:
    """把 Worker owner 限定在当前异步执行树，退出后必定复位。"""
    token = _WORKER_INSTANCE.set(worker_instance)
    try:
        yield
    finally:
        _WORKER_INSTANCE.reset(token)


class StructuredLogTraceSink:
    """每个事件一条结构化日志记录。"""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        worker_instance: str | None = None,
    ) -> None:
        self._logger = logger if logger is not None else _LOGGER
        self._worker_instance = worker_instance

    async def emit(self, event: TraceEvent, *, delivery: Delivery) -> None:
        """写出一条记录。

        字段经 ``extra`` 传出，消息体恒为常量：把取值拼进消息会让脱敏与限长在
        格式化那一步失效（与拒绝路径不回显输入是同一条不变量）。
        """
        validate_delivery(event, delivery)
        state = _DELIVERY_STATE.get(delivery)
        if state is None:
            raise ValueError("durable delivery must be resolved before logging")
        error = event.error
        error_summary = (
            None
            if error is None
            else {
                "code": error.code,
                "category": error.category.value,
                "retryable": error.retryable,
                "message_key": error.message_key,
                "cause_ref": error.cause_ref,
            }
        )
        try:
            self._logger.info(
                "trace event",
                extra={
                    "trace_id": event.trace_id,
                    "task_id": event.task_id,
                    "event_id": event.event_id,
                    "stage": event.stage.value,
                    "outcome": event.outcome.value,
                    "step_id": event.step_id,
                    "capability_id": event.capability_id,
                    "policy_revision": event.policy_revision,
                    "attempt_number": event.attempt_number,
                    "delivery_state": state,
                    "error": error_summary,
                    # detail 已在契约层脱敏；这里原样透出，不再二次拼装。
                    "detail": dict(event.detail),
                    "worker_instance": (
                        self._worker_instance
                        if self._worker_instance is not None
                        else _WORKER_INSTANCE.get()
                    ),
                },
            )
        except Exception:
            # logging 是诊断面，不得反向改变审计写入或任务生命周期的结局。
            return

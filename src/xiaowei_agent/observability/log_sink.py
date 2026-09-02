"""把 trace 事件写进标准库 logging。

只依赖 ``contracts`` 与标准库 ``logging``：**不引入任何采集后端**。事件的
``detail`` 已在契约层脱敏并限长，因此本模块**不再自己拼一份原文**——那正是绕过
脱敏边界最容易发生的地方。
"""

import logging
from typing import Final

from xiaowei_agent.contracts import TraceEvent

LOGGER_NAME: Final[str] = "xiaowei_agent.trace"

_LOGGER: Final[logging.Logger] = logging.getLogger(LOGGER_NAME)


class StructuredLogTraceSink:
    """每个事件一条结构化日志记录。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else _LOGGER

    def emit(self, event: TraceEvent) -> None:
        """写出一条记录。

        字段经 ``extra`` 传出，消息体恒为常量：把取值拼进消息会让脱敏与限长在
        格式化那一步失效（与拒绝路径不回显输入是同一条不变量）。
        """
        self._logger.info(
            "trace event",
            extra={
                "trace_id": event.trace_id,
                "task_id": event.task_id,
                "stage": event.stage.value,
                "outcome": event.outcome.value,
                "step_id": event.step_id,
                "capability_id": event.capability_id,
                "policy_revision": event.policy_revision,
                # detail 已在契约层脱敏；这里原样透出，不再二次拼装。
                "detail": dict(event.detail),
            },
        )

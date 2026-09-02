"""trace / audit 事件出口契约。M2 只声明，不实现采集后端。"""

from typing import Protocol

from xiaowei_agent.contracts import TraceEvent


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...

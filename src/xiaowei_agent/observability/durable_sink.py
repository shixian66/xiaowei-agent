"""把独立 trace 事件先写审计台账，再记录已知的投递结局。"""

from xiaowei_agent.contracts import TraceEvent
from xiaowei_agent.observability.log_sink import StructuredLogTraceSink
from xiaowei_agent.observability.sink import (
    AuditEventWriter,
    Delivery,
    TraceSink,
    delivery_for_write_exception,
    validate_delivery,
)


class DurableTraceSink:
    """``LOG_AND_DURABLE`` 的唯一执行者；不自动重放 append-only 事件。"""

    def __init__(
        self,
        *,
        writer: AuditEventWriter,
        log_sink: TraceSink | None = None,
        worker_instance: str | None = None,
    ) -> None:
        self._writer = writer
        self._log = (
            StructuredLogTraceSink(worker_instance=worker_instance)
            if log_sink is None
            else log_sink
        )

    async def emit(self, event: TraceEvent, *, delivery: Delivery) -> None:
        validate_delivery(event, delivery)
        if delivery is not Delivery.LOG_AND_DURABLE:
            await self._log.emit(event, delivery=delivery)
            return

        try:
            await self._writer.record_audit_event(event=event)
        except Exception as exc:
            await self._log.emit(event, delivery=delivery_for_write_exception(exc))
            raise
        await self._log.emit(event, delivery=Delivery.COMMAND_COMMITTED)

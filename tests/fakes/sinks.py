"""记录型 TraceSink 与事件夹具。"""

import datetime as dt
from collections.abc import Mapping
from typing import Any

from xiaowei_agent.contracts import PipelineStage, StageOutcome, TraceEvent

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)


def make_event(
    *,
    stage: PipelineStage,
    outcome: StageOutcome = StageOutcome.OK,
    detail: Mapping[str, str] | None = None,
) -> TraceEvent:
    return TraceEvent(
        event_id="e1",
        trace_id="0" * 32,
        task_id="task-1",
        stage=stage,
        outcome=outcome,
        occurred_at=_AT,
        capability_id="starrocks.slow_query.diagnose",
        step_id="s1",
        policy_revision="policy-2026-09-01",
        error=None,
        detail=dict(detail or {}),
    )


class RecordingTraceSink:
    """把事件收进列表，使阶段顺序与归因成为可断言事实。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)


class SpyEvidenceLedger:
    """包装任意 ledger 并记录读写次数。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.appends = 0
        self.loads = 0
        self.gets = 0

    async def append(self, *, task_id: str, envelope: Any) -> str:
        self.appends += 1
        return await self._inner.append(task_id=task_id, envelope=envelope)

    async def load(self, *, task_id: str) -> tuple[Any, ...]:
        self.loads += 1
        return await self._inner.load(task_id=task_id)

    async def get(self, *, task_id: str, evidence_id: str) -> Any:
        self.gets += 1
        return await self._inner.get(task_id=task_id, evidence_id=evidence_id)

    async def clear(self, *, task_id: str) -> None:
        """清空某个任务的证据。

        **只在测试里存在**：用于反证"Runtime 确实按引用从 ledger 读回"——清空后
        同一次 outcome 必须渲染成"无证据"。
        """
        self._inner._entries.pop(task_id, None)

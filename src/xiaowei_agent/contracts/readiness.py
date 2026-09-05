"""入口层可消费的最小 readiness 契约。"""

from typing import Protocol

from xiaowei_agent.contracts.base import Contract


class ReadinessReport(Contract):
    database_ok: bool
    revision_matches_head: bool
    assembled: bool


class ReadinessProbe(Protocol):
    async def check(self) -> ReadinessReport: ...

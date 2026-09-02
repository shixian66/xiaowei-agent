"""Runner 契约。签名与 ARCHITECTURE §5.6 一致；M2 不实现任何真实 Runner。"""

from typing import Protocol

from xiaowei_agent.contracts import ExternalInput, TaskOutcome


class WorkflowRunner(Protocol):
    async def start(self, task_id: str) -> TaskOutcome: ...

    async def resume(
        self, task_id: str, external_input: ExternalInput | None = None
    ) -> TaskOutcome: ...

"""最小同步 fake runner 测试替身（DEVELOPMENT_PLAN §7 M2 明文要求置于 runners/）。

**它不执行任何步骤**——步骤执行是 M3 的 DeterministicStepRunner。它只按脚本驱动
TaskStore 走到一个终态，用于验证 Runner 契约、终态语义与 ``TaskOutcome`` 形状。
因此它既不持有 ToolGateway，也不接触 adapter：**adapter 调用次数恒为 0**。
"""

from typing import Final

from xiaowei_agent.contracts import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ExternalInput,
    TaskOutcome,
    TaskStatus,
)
from xiaowei_agent.persistence.store import TaskIdCarryingError, TaskStore

IS_FAKE: Final[bool] = True


class TerminalOrLeasedTaskError(TaskIdCarryingError, RuntimeError):
    """任务已终态，或租约被他人持有——两种情况下本 Runner 都不应推进它。

    ``task_id`` 放结构化属性、不进 ``str(exc)``：理由见 :class:`TaskIdCarryingError`。
    """

    def __init__(self, *, task_id: str) -> None:
        super().__init__("task is terminal or leased by another owner", task_id=task_id)


class ScriptedRunner:
    def __init__(
        self,
        store: TaskStore,
        *,
        outcome_status: TaskStatus,
        terminal_reason: str | None = None,
        owner: str = "scripted-runner",
    ) -> None:
        if outcome_status not in TERMINAL_STATUSES:
            raise ValueError("ScriptedRunner requires a terminal outcome status")
        # 从 RUNNING 不可达的终态（如 REJECTED）必须在构造时就拒绝，否则要跑到
        # 最后一步迁移才失败。
        if outcome_status not in ALLOWED_TRANSITIONS[TaskStatus.RUNNING]:
            raise ValueError(
                f"{outcome_status.value} is not reachable from RUNNING; "
                "ScriptedRunner only drives the running → terminal edge"
            )
        if not owner or owner != owner.strip():
            raise ValueError("owner must be a non-empty, unpadded string")
        self._store = store
        self._status = outcome_status
        self._reason = terminal_reason
        self._owner = owner

    async def start(self, task_id: str) -> TaskOutcome:
        return await self._drive(task_id, path=(TaskStatus.PLANNING, TaskStatus.RUNNING))

    async def resume(
        self, task_id: str, external_input: ExternalInput | None = None
    ) -> TaskOutcome:
        return await self._drive(task_id, path=(TaskStatus.RUNNING,))

    async def _drive(self, task_id: str, *, path: tuple[TaskStatus, ...]) -> TaskOutcome:
        lease = await self._store.acquire_lease(
            task_id=task_id, owner=self._owner, ttl_seconds=30
        )
        if lease is None:
            # 终态任务不可 acquire lease，因此这也是"任务已结束"的信号。
            raise TerminalOrLeasedTaskError(task_id=task_id)
        record = await self._store.get(task_id)
        for status in (*path, self._status):
            if record.status is status:
                continue
            result = await self._store.transition(
                task_id=task_id,
                expected_version=record.version,
                to_status=status,
                fencing_token=lease.fencing_token,
                terminal_reason=self._reason if status is self._status else None,
            )
            if not result.applied:
                raise RuntimeError(f"transition rejected: {result.rejection}")
            record = result.winner  # 必须采纳存储层 winner，不用本地旧对象
        return TaskOutcome(
            task_id=task_id,
            status=record.status,
            terminal_reason=record.terminal_reason,
            evidence_refs=(),
            render_ref=None,
        )

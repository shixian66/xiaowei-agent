"""PlanStore：只存 plan + target 的可寻址存储。

**为什么新增 port 而不是扩 TaskStore**：``TaskStore`` 的交互形状在 M2 被明文冻结
供 M3/M4 共用。resume 时重算 ``plan_hash`` 需要拿回**当初那份**计划与目标，这是一项
**新能力**，不是 M2 形状的缺陷。用新 port 表达，M4 并进 PostgreSQL 时只换实现，
不动 M2 已验收的契约。

**边界**：``StoredPlan`` 的字段集恰为两项，``PlanStore`` 的方法集恰为两个。用契约
表达比用注释约定更强——PlanStore 不得变成状态、审批、证据或 render 的第二真源。
"""

from collections.abc import Mapping
from typing import Any, Final, Protocol, cast

from xiaowei_agent.contracts import (
    PLAN_SCHEMA_VERSION,
    Contract,
    ExecutionPlan,
    ResolvedTarget,
)
from xiaowei_agent.persistence.memory import InMemoryPersistenceState

PLAN_SCHEMA_VERSION_UNSUPPORTED_REASON: Final[str] = "plan.schema_version_unsupported"


class PlanStoreError(Exception):
    """携带 ``task_id`` 但**不把它放进 ``str(exc)``** 的错误基类。

    与 ``TaskIdCarryingError`` 同一模式：诊断值放结构化属性，默认错误文本恒为常量，
    不会被日志或错误响应顺手带出去。
    """

    def __init__(self, message: str, *, task_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id


class PlanNotFoundError(PlanStoreError, LookupError):
    """任务没有已保存的计划。"""


class PlanConflictError(PlanStoreError):
    """同一 task_id 已存在**不同**的计划或目标。"""


class PlanSchemaVersionUnsupportedError(PlanStoreError):
    """已持久化计划不是当前 schema；调用方必须 fail-closed 新建任务。"""

    def __init__(self, *, task_id: str) -> None:
        super().__init__("stored plan schema version is unsupported", task_id=task_id)
        self.reason_code = PLAN_SCHEMA_VERSION_UNSUPPORTED_REASON


def reject_unsupported_plan_schema(
    payload: Mapping[str, Any], *, task_id: str
) -> None:
    """在构造 ``ExecutionPlan`` 前拒绝旧 schema，避免嵌套字段误分类。"""
    if payload.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanSchemaVersionUnsupportedError(task_id=task_id)


class StoredPlan(Contract):
    """PlanStore 存储的**全部**内容。

    字段集恰为两项是刻意的：PlanStore 不得变成状态、审批、证据或 render 的第二
    真源。由 ``test_plan_store_holds_nothing_but_plan_and_target`` 承重。
    """

    plan: ExecutionPlan
    target: ResolvedTarget


class PlanStore(Protocol):
    async def save(
        self, *, task_id: str, plan: ExecutionPlan, target: ResolvedTarget
    ) -> None:
        """幂等保存。

        同一 task_id 重复保存**不同**的计划必须拒绝——计划一旦被执行或送审，它就是
        这次任务的事实，静默覆盖等于给自己开一条计划漂移的口子：审批批的是旧计划，
        执行的是新计划。

        :raises PlanConflictError: 同一 task_id 已存在不同的计划或目标。
        """

    async def load(self, *, task_id: str) -> StoredPlan:
        """:raises PlanNotFoundError: 任务没有已保存的计划。"""


class InMemoryPlanStore:
    """单进程实现。

    **只有单进程保证**：跨进程原子性与崩溃恢复要到 M4 的 PostgreSQL 实现才可证。
    一把 ``asyncio.Lock`` 串行化写入，与 ``InMemoryTaskStore`` 同一取舍。
    """

    def __init__(self, *, state: InMemoryPersistenceState | None = None) -> None:
        self._state = InMemoryPersistenceState() if state is None else state
        self._plans = self._state.plans
        self._lock = self._state.lock

    async def save(
        self, *, task_id: str, plan: ExecutionPlan, target: ResolvedTarget
    ) -> None:
        record = StoredPlan(plan=plan, target=target)
        async with self._lock:
            existing = self._plans.get(task_id)
            if existing is not None and existing != record:
                raise PlanConflictError(
                    "a different plan is already stored for this task", task_id=task_id
                )
            self._plans[task_id] = record

    async def load(self, *, task_id: str) -> StoredPlan:
        async with self._lock:
            stored = self._plans.get(task_id)
        if stored is None:
            raise PlanNotFoundError("no plan stored for this task", task_id=task_id)
        return cast(StoredPlan, stored)

"""PlanStore：只存 plan + target 的可寻址存储。

存在的理由是 resume 时要重算 ``plan_hash`` 与 ``target_fingerprint``，而重算需要
先拿回**当初那份**计划与目标。字段集恰为两项是刻意的：PlanStore 不得变成状态、
审批、证据或 render 的第二真源。
"""

import pytest
from tests.fakes.admission import TARGET, slow_query_plan

from xiaowei_agent.persistence.plans import (
    InMemoryPlanStore,
    PlanConflictError,
    PlanNotFoundError,
    PlanStore,
    StoredPlan,
)
from xiaowei_agent.planning import compute_plan_hash

_TASK = "task-1"


@pytest.fixture
def store() -> InMemoryPlanStore:
    return InMemoryPlanStore()


def test_plan_store_holds_nothing_but_plan_and_target() -> None:
    assert set(StoredPlan.model_fields) == {"plan", "target"}
    assert {m for m in dir(PlanStore) if not m.startswith("_")} == {"save", "load"}


async def test_saved_plan_round_trips(store: InMemoryPlanStore) -> None:
    plan = slow_query_plan()
    await store.save(task_id=_TASK, plan=plan, target=TARGET)
    stored = await store.load(task_id=_TASK)
    assert stored.plan == plan
    assert stored.target == TARGET


async def test_reloaded_plan_recomputes_to_the_same_hash(store: InMemoryPlanStore) -> None:
    """往返必须保 hash：否则 resume 的漂移检测会把每一次恢复都判成漂移。"""
    plan = slow_query_plan()
    await store.save(task_id=_TASK, plan=plan, target=TARGET)
    stored = await store.load(task_id=_TASK)
    assert compute_plan_hash(stored.plan) == compute_plan_hash(plan)


async def test_saving_the_same_plan_twice_is_idempotent(store: InMemoryPlanStore) -> None:
    plan = slow_query_plan()
    await store.save(task_id=_TASK, plan=plan, target=TARGET)
    await store.save(task_id=_TASK, plan=plan, target=TARGET)
    assert (await store.load(task_id=_TASK)).plan == plan


async def test_saving_a_different_plan_for_the_same_task_is_refused(
    store: InMemoryPlanStore,
) -> None:
    """计划一旦被执行或送审，它就是这次任务的事实。

    静默覆盖等于给自己开一条计划漂移的口子：审批批的是旧计划，执行的是新计划。
    """
    plan = slow_query_plan()
    await store.save(task_id=_TASK, plan=plan, target=TARGET)
    other = plan.model_copy(update={"policy_revision": "policy-2026-10-01"})
    with pytest.raises(PlanConflictError):
        await store.save(task_id=_TASK, plan=other, target=TARGET)
    assert (await store.load(task_id=_TASK)).plan == plan


async def test_saving_a_different_target_for_the_same_task_is_refused(
    store: InMemoryPlanStore,
) -> None:
    plan = slow_query_plan()
    await store.save(task_id=_TASK, plan=plan, target=TARGET)
    other = TARGET.model_copy(update={"environment_id": "test"})
    with pytest.raises(PlanConflictError):
        await store.save(task_id=_TASK, plan=plan, target=other)


async def test_loading_an_unknown_task_raises(store: InMemoryPlanStore) -> None:
    with pytest.raises(PlanNotFoundError):
        await store.load(task_id="never-saved")


async def test_conflict_error_does_not_echo_the_task_id(store: InMemoryPlanStore) -> None:
    """与 TaskIdCarryingError 同一模式：诊断值放结构化属性，不进错误文本。"""
    canary = "task-canary-8821"
    plan = slow_query_plan()
    await store.save(task_id=canary, plan=plan, target=TARGET)
    other = plan.model_copy(update={"policy_revision": "policy-2026-10-01"})
    with pytest.raises(PlanConflictError) as err:
        await store.save(task_id=canary, plan=other, target=TARGET)
    assert canary not in str(err.value)
    assert err.value.task_id == canary


async def test_tasks_are_isolated_from_each_other(store: InMemoryPlanStore) -> None:
    plan = slow_query_plan()
    await store.save(task_id="a", plan=plan, target=TARGET)
    with pytest.raises(PlanNotFoundError):
        await store.load(task_id="b")

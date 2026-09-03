"""B3：``plan_hash`` / ``target_fingerprint`` **不落库**，漂移由调用方当下重算。

存下指纹再和存下的计划比，两侧都来自存储，检查恒真——与 §8.4 的空洞检查同一根因，
只是换了一张表。ADR-009 已裁定这两个指纹不是 ``ExecutionPlan`` 的字段，绑定值只存于
``ApprovalRequest``。

因此漂移检测的正确形状是：**读回计划 → 当下重算指纹 → 与审批里绑定的那个值比**。
本文件用直接篡改存储内容的方式证明这条链真的会发现漂移。
"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.fakes.admission import TARGET, slow_query_plan

from xiaowei_agent.persistence.schema import TASK_PLANS
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint

pytestmark = pytest.mark.security

_TASK = "task-drift-1"


def test_no_fingerprint_is_stored_alongside_the_plan() -> None:
    """列集合恰为三项。多存一个指纹就多一条"两侧同源"的路径。"""
    assert {column.name for column in TASK_PLANS.columns} == {"task_id", "plan", "target"}


async def test_a_tampered_plan_recomputes_to_a_different_hash(
    plan_store: Any, clean_database: AsyncEngine
) -> None:
    """篡改存储中的计划后，调用方**当下重算**的 plan_hash 必须不匹配。"""
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    approved_hash = compute_plan_hash(plan)

    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE task_plans"
                " SET plan = jsonb_set(plan, '{policy_revision}', '\"policy-2026-12-31\"')"
                " WHERE task_id = :task_id"
            ),
            {"task_id": _TASK},
        )

    reloaded = await plan_store.load(task_id=_TASK)
    assert compute_plan_hash(reloaded.plan) != approved_hash


async def test_an_untampered_plan_recomputes_to_the_same_hash(
    plan_store: Any,
) -> None:
    """反面：正常往返必须**保**指纹。

    没有这条，上面那条可能只是因为"往返本身就会改变指纹"而通过——那样每一次恢复
    都会被判成漂移，漂移检测立刻会被人关掉。
    """
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    reloaded = await plan_store.load(task_id=_TASK)
    assert compute_plan_hash(reloaded.plan) == compute_plan_hash(plan)
    assert compute_target_fingerprint(reloaded.target) == compute_target_fingerprint(TARGET)


async def test_a_tampered_target_recomputes_to_a_different_fingerprint(
    plan_store: Any, clean_database: AsyncEngine
) -> None:
    plan = slow_query_plan()
    await plan_store.save(task_id=_TASK, plan=plan, target=TARGET)
    approved = compute_target_fingerprint(TARGET)

    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE task_plans"
                " SET target = jsonb_set(target, '{environment_id}', '\"prod\"')"
                " WHERE task_id = :task_id"
            ),
            {"task_id": _TASK},
        )

    reloaded = await plan_store.load(task_id=_TASK)
    assert compute_target_fingerprint(reloaded.target) != approved

"""``PlanStore`` —— **内存绑定** + 与实现无关的契约断言。

行为用例定义在 ``tests/suites/plan_store.py``，PostgreSQL 绑定在 ``tests/integration/``。
留在本文件的那条断言与存储实现无关：它检查的是**契约形状**，绑到第二个实现上只会
重复跑一遍同一个 ``dir()``。
"""

from tests.suites.plan_store import PLAN_STORE_CASES, bind

from xiaowei_agent.persistence.plans import PlanStore, StoredPlan

bind(globals(), PLAN_STORE_CASES)


def test_plan_store_holds_nothing_but_plan_and_target() -> None:
    """字段集恰为两项：PlanStore 不得变成状态、审批、证据或 render 的第二真源。"""
    assert set(StoredPlan.model_fields) == {"plan", "target"}
    assert {m for m in dir(PlanStore) if not m.startswith("_")} == {"save", "load"}

"""``PlanStore`` —— **内存绑定** + 与实现无关的契约断言。

行为用例定义在 ``tests/suites/plan_store.py``，PostgreSQL 绑定在 ``tests/integration/``。
留在本文件的那条断言与存储实现无关：它检查的是**契约形状**，绑到第二个实现上只会
重复跑一遍同一个 ``dir()``。
"""

from tests.suites.plan_store import PLAN_STORE_CASES, bind

from xiaowei_agent.persistence.plans import (
    PlanSchemaVersionUnsupportedError,
    PlanStore,
    StoredPlan,
    reject_unsupported_plan_schema,
)

bind(globals(), PLAN_STORE_CASES)


def test_plan_store_holds_nothing_but_plan_and_target() -> None:
    """字段集恰为两项：PlanStore 不得变成状态、审批、证据或 render 的第二真源。"""
    assert set(StoredPlan.model_fields) == {"plan", "target"}
    assert {m for m in dir(PlanStore) if not m.startswith("_")} == {"save", "load"}


def test_raw_stored_v1_plan_is_rejected_before_contract_decode() -> None:
    try:
        reject_unsupported_plan_schema({"plan_schema_version": 1}, task_id="task-1")
    except PlanSchemaVersionUnsupportedError as exc:
        assert exc.reason_code == "plan.schema_version_unsupported"
        assert "task-1" not in str(exc)
    else:
        raise AssertionError("old stored plan schema must be rejected")

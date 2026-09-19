"""``PlanStore`` —— **PostgreSQL 绑定**。"""

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.fakes.admission import TARGET
from tests.suites.plan_store import PLAN_STORE_CASES, bind

from xiaowei_agent.persistence.rows import dump_contract
from xiaowei_agent.persistence.schema import TASK_PLANS

bind(globals(), PLAN_STORE_CASES)


def _v1_plan_payload_without_read_class() -> dict[str, object]:
    return {
        "plan_schema_version": 1,
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "steps": [
            {
                "step_id": "s1",
                "operation": "list_slow_queries",
                "typed_arguments": {"window_minutes": 30},
                "depends_on": [],
                "side_effect": False,
                "effect_class": "read",
            }
        ],
        "policy_profile": "readonly.default",
        "policy_revision": "policy-2026-09-01",
        "budget": {
            "max_steps": 2,
            "max_tool_calls": 2,
            "max_model_tokens": 8000,
        },
    }


async def test_v1_plan_payload_is_rejected_with_closed_schema_reason(
    plan_store, clean_database: AsyncEngine
) -> None:
    task_id = "task-old-plan-schema"
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.insert(TASK_PLANS).values(
                task_id=task_id,
                plan=_v1_plan_payload_without_read_class(),
                target=dump_contract(TARGET),
            )
        )

    with pytest.raises(Exception) as caught:
        await plan_store.load(task_id=task_id)
    assert type(caught.value).__name__ == "PlanSchemaVersionUnsupportedError"
    assert caught.value.reason_code == "plan.schema_version_unsupported"

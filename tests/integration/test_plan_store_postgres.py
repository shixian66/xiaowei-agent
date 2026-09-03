"""``PlanStore`` —— **PostgreSQL 绑定**。"""

from tests.suites.plan_store import PLAN_STORE_CASES, bind

bind(globals(), PLAN_STORE_CASES)

"""``TaskStore`` 交互形状 —— **PostgreSQL 绑定**。

与 ``tests/contract/test_task_store_contract.py`` 跑的是**同一批函数对象**，只是
``store`` fixture 指向 ``PostgresTaskStore``。这正是判定标准 1 的落实方式。

无 DSN 时整组跳过；有 DSN 时一条都不许跳（见 conftest 的 ``pytest_sessionfinish``）。
"""

from tests.suites.task_store import CONTRACT_CASES, bind

bind(globals(), CONTRACT_CASES)

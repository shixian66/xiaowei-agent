"""``TaskStore`` 交互形状 —— **内存绑定**。

用例定义在 ``tests/suites/task_store.py``，本文件只负责把它们绑到 ``store`` fixture
指向的 ``InMemoryTaskStore``。PostgreSQL 绑定在 ``tests/integration/``，跑的是同一批
函数对象——这正是"同一套行为用例在两个实现上逐条通过"（M4 §1 判定标准 1）的落实
方式。

**不要在本文件里直接写用例**：写在这里的用例只会跑在内存实现上，而没有任何东西会
报错。新增用例请加进套件模块的分组，由 ``test_task_store_bindings.py`` 承重。
"""

from tests.suites.task_store import CONTRACT_CASES, bind

bind(globals(), CONTRACT_CASES)

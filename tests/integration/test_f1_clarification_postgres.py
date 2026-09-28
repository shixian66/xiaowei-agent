"""F1 目标选择澄清的 SQL 分类与延期 —— **PostgreSQL 绑定**（设计 §9.4 第 2 项）。

父任务行锁、SQL 行 ``FOR UPDATE`` 与子任务插入在同一事务；失败时整体回滚。
"""

from tests.suites.task_store import F1_CLARIFICATION_CASES, bind

bind(globals(), F1_CLARIFICATION_CASES)

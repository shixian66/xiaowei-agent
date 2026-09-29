"""F1 目标选择澄清的 SQL 分类与延期 —— **内存绑定**（设计 §9.4 第 2 项）。"""

from tests.suites.task_store import F1_CLARIFICATION_CASES, bind

bind(globals(), F1_CLARIFICATION_CASES)

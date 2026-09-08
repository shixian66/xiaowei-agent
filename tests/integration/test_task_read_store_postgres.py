"""M7 TaskStore 作用域读取契约 —— PostgreSQL 实现绑定。"""

from tests.suites.task_store import READ_CASES, bind

bind(globals(), READ_CASES)

"""存储层的终态保护 —— **PostgreSQL 绑定**。

只绑存储层那两条。终态集合、迁移表、``TaskOutcome`` 的终态约束与存储实现无关，
留在 ``tests/security/test_terminal_protection.py``。
"""

import pytest
from tests.suites.task_store import TERMINAL_PROTECTION_CASES, bind

pytestmark = pytest.mark.security

bind(globals(), TERMINAL_PROTECTION_CASES)

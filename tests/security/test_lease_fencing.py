"""租约与 fencing 的闭合规则 —— **内存绑定**。

只在"调用方传了 token"时才校验，等于不传即绕过。套件里把六种组合全部钉死。

用例定义在 ``tests/suites/task_store.py``；本文件提供 ``security`` marker 与内存
``store``。PostgreSQL 绑定在 ``tests/integration/``，同样带 ``security`` marker，
因此 ``python -m pytest -m security -q`` 覆盖两个实现。

**不要在本文件里直接写用例**：见 ``test_task_store_bindings.py``。
"""

import pytest
from tests.suites.task_store import LEASE_FENCING_CASES, bind

pytestmark = pytest.mark.security

bind(globals(), LEASE_FENCING_CASES)

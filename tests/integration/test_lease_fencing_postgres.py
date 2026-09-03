"""租约与 fencing 的闭合规则 —— **PostgreSQL 绑定**。

保留 ``security`` marker，因此 ``python -m pytest -m security -q`` 覆盖**两个**实现。
"""

import pytest
from tests.suites.task_store import LEASE_FENCING_CASES, bind

pytestmark = pytest.mark.security

bind(globals(), LEASE_FENCING_CASES)

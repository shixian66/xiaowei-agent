"""Admin 身份查询的内存实现共享套件绑定。"""

from types import SimpleNamespace

import pytest
from tests.suites.admin_identity_queries import ADMIN_IDENTITY_QUERY_CASES, bind

from xiaowei_agent.contracts.activation import ActivationLookup
from xiaowei_agent.persistence.fake import (
    InMemoryActivationStore,
    InMemoryAdminAuditStore,
    InMemoryUserDirectoryStore,
)


@pytest.fixture
def admin_query_bundle(clock, memory_state):
    class _Bundle(SimpleNamespace):
        @staticmethod
        def activation_lookup(request_id: str) -> ActivationLookup:
            return ActivationLookup(
                request_id=request_id,
                tenant_id="tenant-a",
                environment_id="env-a",
            )

    return _Bundle(
        directory=InMemoryUserDirectoryStore(clock=clock, state=memory_state),
        activations=InMemoryActivationStore(clock=clock, state=memory_state),
        audit=InMemoryAdminAuditStore(clock=clock, state=memory_state),
        clock=clock,
    )


bind(globals(), ADMIN_IDENTITY_QUERY_CASES)

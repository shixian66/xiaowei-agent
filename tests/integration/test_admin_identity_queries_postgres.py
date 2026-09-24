"""Admin 身份查询的真实 PostgreSQL 共享套件绑定与查询次数证明。"""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from tests.suites.admin_identity_queries import (
    ADMIN_IDENTITY_QUERY_CASES,
    ENVIRONMENT,
    TENANT,
    _create_user,
    bind,
)

from xiaowei_agent.contracts.activation import ActivationLookup
from xiaowei_agent.contracts.identity import AdminUserListQuery
from xiaowei_agent.persistence.postgres import (
    PostgresActivationStore,
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
)


@pytest.fixture
def admin_query_bundle(clean_database, clock):
    class _Bundle(SimpleNamespace):
        @staticmethod
        def activation_lookup(request_id: str) -> ActivationLookup:
            return ActivationLookup(
                request_id=request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
            )

    return _Bundle(
        directory=PostgresUserDirectoryStore(engine=clean_database, clock=clock),
        activations=PostgresActivationStore(engine=clean_database, clock=clock),
        audit=PostgresAdminAuditStore(engine=clean_database, clock=clock),
        clock=clock,
        engine=clean_database,
    )


bind(globals(), ADMIN_IDENTITY_QUERY_CASES)


async def test_admin_user_page_is_one_batched_select(admin_query_bundle) -> None:
    for index in range(21, 26):
        await _create_user(
            admin_query_bundle,
            index=index,
            actor=f"user-{index}@example.test",
            bind_feishu=index % 2 == 0,
        )

    statements: list[str] = []

    def _record_statement(*args: object) -> None:
        statements.append(str(args[2]))

    sa.event.listen(
        admin_query_bundle.engine.sync_engine,
        "before_cursor_execute",
        _record_statement,
    )
    try:
        page = await admin_query_bundle.directory.list_admin_users(
            query=AdminUserListQuery(
                tenant_id=TENANT, environment_id=ENVIRONMENT, limit=100
            )
        )
    finally:
        sa.event.remove(
            admin_query_bundle.engine.sync_engine,
            "before_cursor_execute",
            _record_statement,
        )

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(page.items) == 5
    assert len(selects) == 1

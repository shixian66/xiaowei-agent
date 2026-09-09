"""WebSessionStore 的真实 PostgreSQL 并发与迁移实证。"""

import pytest
from tests.suites.web_session_store import WEB_SESSION_STORE_CASES, bind

from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError
from xiaowei_agent.persistence.postgres import PostgresWebSessionStore
from xiaowei_agent.persistence.web_session import (
    IssueOAuthStateCommand,
    RotateWebSessionCommand,
)


@pytest.fixture
def web_sessions(clock, clean_database):
    return PostgresWebSessionStore(engine=clean_database, clock=clock)


bind(globals(), WEB_SESSION_STORE_CASES)


async def test_rev_0007_downgrade_rejects_web_auth_data_without_authorization(
    web_sessions, clean_database, alembic_runners
) -> None:
    await web_sessions.issue_oauth_state(
        command=IssueOAuthStateCommand(state_digest="a" * 64, ttl_seconds=60)
    )
    await web_sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest="b" * 64,
            subject_ref="subject-alice",
            ttl_seconds=3600,
        )
    )
    run_upgrade, run_downgrade = alembic_runners

    with pytest.raises(MigrationSafetyError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0006_channels")

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0006_channels", True)
        await connection.run_sync(run_upgrade)

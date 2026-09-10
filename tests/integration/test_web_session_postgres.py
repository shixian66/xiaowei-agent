"""WebSessionStore 的真实 PostgreSQL 并发与迁移实证。"""

import datetime as dt

import pytest
import sqlalchemy as sa
from tests.suites.web_session_store import WEB_SESSION_STORE_CASES, bind

from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError
from xiaowei_agent.persistence.postgres import PostgresWebSessionStore
from xiaowei_agent.persistence.schema import WEB_OAUTH_STATES
from xiaowei_agent.persistence.web_session import (
    IssueOAuthStateCommand,
    OAuthStateCapacityError,
    RotateWebSessionCommand,
)


@pytest.fixture
def web_sessions(clock, clean_database):
    return PostgresWebSessionStore(engine=clean_database, clock=clock)


@pytest.fixture
def bounded_web_sessions(clock, clean_database):
    return PostgresWebSessionStore(
        engine=clean_database,
        clock=clock,
        oauth_state_capacity=2,
    )


@pytest.fixture
def oauth_state_digests(clean_database):
    async def load() -> set[str]:
        async with clean_database.connect() as connection:
            rows = await connection.execute(
                sa.select(WEB_OAUTH_STATES.c.state_digest)
            )
        return set(rows.scalars())

    return load


bind(globals(), WEB_SESSION_STORE_CASES)


async def test_capacity_rejection_commits_expired_state_cleanup(
    bounded_web_sessions, clean_database, clock
) -> None:
    for digest in ("1" * 64, "2" * 64):
        await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(state_digest=digest, ttl_seconds=60)
        )
    expired_digest = "3" * 64
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.insert(WEB_OAUTH_STATES).values(
                state_digest=expired_digest,
                issued_at=clock() - dt.timedelta(seconds=2),
                expires_at=clock() - dt.timedelta(seconds=1),
                consumed_at=None,
            )
        )

    with pytest.raises(OAuthStateCapacityError):
        await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest="4" * 64,
                ttl_seconds=60,
            )
        )

    async with clean_database.connect() as connection:
        remaining = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(WEB_OAUTH_STATES)
            .where(WEB_OAUTH_STATES.c.state_digest == expired_digest)
        )
    assert remaining == 0


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

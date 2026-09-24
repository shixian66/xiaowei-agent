"""WebSessionStore 的真实 PostgreSQL 并发与迁移实证。"""

import datetime as dt

import pytest
import sqlalchemy as sa
from tests.suites.web_session_store import (
    WEB_SESSION_STORE_CASES,
    bind,
    oauth_test_issue,
)

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.web_navigation import WebReturnIntent, WebReturnIntentKind
from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError
from xiaowei_agent.persistence.postgres import PostgresWebSessionStore
from xiaowei_agent.persistence.schema import (
    WEB_OAUTH_LOGIN_CONTEXTS,
    WEB_OAUTH_STATES,
    WEB_OAUTH_TEST_CONTEXTS,
)
from xiaowei_agent.persistence.web_session import (
    IssueOAuthLoginStateCommand,
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


@pytest.fixture
def oauth_login_context_digests(clean_database):
    async def load() -> set[str]:
        async with clean_database.connect() as connection:
            rows = await connection.execute(
                sa.select(WEB_OAUTH_LOGIN_CONTEXTS.c.state_digest)
            )
        return set(rows.scalars())

    return load


@pytest.fixture
def delete_oauth_login_context(clean_database):
    async def delete(state_digest: str) -> None:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.delete(WEB_OAUTH_LOGIN_CONTEXTS).where(
                    WEB_OAUTH_LOGIN_CONTEXTS.c.state_digest == state_digest
                )
            )

    return delete


@pytest.fixture
def oauth_test_context_digests(clean_database):
    async def load() -> set[str]:
        async with clean_database.connect() as connection:
            rows = await connection.execute(
                sa.select(WEB_OAUTH_TEST_CONTEXTS.c.state_digest)
            )
        return set(rows.scalars())

    return load


@pytest.fixture
def delete_oauth_test_context(clean_database):
    async def delete(state_digest: str) -> None:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.delete(WEB_OAUTH_TEST_CONTEXTS).where(
                    WEB_OAUTH_TEST_CONTEXTS.c.state_digest == state_digest
                )
            )

    return delete


bind(globals(), WEB_SESSION_STORE_CASES)


async def test_capacity_rejection_commits_expired_state_cleanup(
    bounded_web_sessions, clean_database, clock
) -> None:
    for digest in ("1" * 64, "2" * 64):
        await bounded_web_sessions.issue_oauth_test_state(
            command=oauth_test_issue(state_digest=digest, ttl_seconds=60)
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
        await bounded_web_sessions.issue_oauth_test_state(
            command=oauth_test_issue(
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
    # 登录 state：rev_0018 的降级守卫对测试 context 一律拒绝，这里只证明 0007 的守卫。
    await web_sessions.issue_oauth_login_state(
        command=IssueOAuthLoginStateCommand(
            state_digest="a" * 64,
            ttl_seconds=60,
            return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
        )
    )
    await web_sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest="b" * 64,
            subject_ref="subject-alice",
            auth_source=IdentitySource.FEISHU,
            public_origin_digest="a1" * 32,
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

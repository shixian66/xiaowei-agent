"""终态激活保留的真实 PostgreSQL 绑定：共享套件、全局锁与审计不受影响。"""

import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa
from tests.suites.activation_store import (
    ACTIVATION_RETENTION_CASES,
    RETENTION,
    bind,
    stored_request,
)

from xiaowei_agent.contracts.activation import ActivationStatus
from xiaowei_agent.persistence.postgres import (
    ACTIVATION_CAPACITY_LOCK_KEY,
    PostgresActivationRetentionStore,
    PostgresActivationStore,
)
from xiaowei_agent.persistence.rows import activation_request_to_row
from xiaowei_agent.persistence.schema import ACTIVATION_REQUESTS


@pytest.fixture
def activation_store(clean_database, clock):
    return PostgresActivationStore(engine=clean_database, clock=clock)


@pytest.fixture
def retention_store(clean_database, clock):
    return PostgresActivationRetentionStore(engine=clean_database, clock=clock)


@pytest.fixture
def seed_activation(clean_database):
    async def seed(request):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.insert(ACTIVATION_REQUESTS).values(
                    **activation_request_to_row(request)
                )
            )

    return seed


bind(globals(), ACTIVATION_RETENTION_CASES)


async def test_retention_waits_for_the_global_activation_lock(
    clean_database, clock, seed_activation
) -> None:
    """清理与容量门抢同一把全局事务锁；持锁期间清理不得推进。"""
    await seed_activation(
        stored_request(
            "rejected-locked",
            status=ActivationStatus.REJECTED,
            terminal_at=clock() - RETENTION - dt.timedelta(days=1),
        )
    )
    retention = PostgresActivationRetentionStore(engine=clean_database, clock=clock)
    async with clean_database.connect() as holder:
        transaction = await holder.begin()
        await holder.execute(
            sa.text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": ACTIVATION_CAPACITY_LOCK_KEY},
        )
        purge = asyncio.ensure_future(retention.purge_expired_terminal())
        done, _ = await asyncio.wait({purge}, timeout=0.5)
        assert not done, "retention ran without the global activation lock"
        await transaction.rollback()
    report = await asyncio.wait_for(purge, timeout=10)

    assert report.rejected_deleted == 1


async def test_retention_leaves_admin_audit_rows_untouched(
    clean_database, clock, seed_activation
) -> None:
    from xiaowei_agent.contracts import (
        AdminAuditAction,
        AdminAuditOutcome,
        AdminAuditTargetKind,
        IdentitySource,
        ProductRole,
    )
    from xiaowei_agent.contracts.admin_audit import (
        AdminAuditCandidate,
        AdminAuditEffect,
        admin_audit_target_digest,
    )
    from xiaowei_agent.persistence.admin_audit import seal
    from xiaowei_agent.persistence.rows import admin_audit_event_to_row
    from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS

    audit = seal(
        AdminAuditCandidate(
            operation_id="op-approve-audited",
            tenant_id="tenant-a",
            environment_id="env-a",
            actor_user_id="admin-1",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
            action=AdminAuditAction.ROLE_ASSIGNED,
            target_kind=AdminAuditTargetKind.USER,
            target_ref_digest=admin_audit_target_digest(
                target_kind=AdminAuditTargetKind.USER, target_ref="u-1"
            ),
            outcome=AdminAuditOutcome.SUCCEEDED,
            effect=AdminAuditEffect(role=ProductRole.OPERATOR),
        ),
        now=clock() - RETENTION - dt.timedelta(days=5),
    )
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.insert(ADMIN_AUDIT_EVENTS).values(**admin_audit_event_to_row(audit))
        )
    await seed_activation(
        stored_request(
            "approved-audited",
            status=ActivationStatus.APPROVED,
            terminal_at=clock() - RETENTION - dt.timedelta(days=1),
        )
    )
    async with clean_database.connect() as connection:
        before = (
            await connection.execute(sa.select(ADMIN_AUDIT_EVENTS))
        ).mappings().all()

    report = await PostgresActivationRetentionStore(
        engine=clean_database, clock=clock
    ).purge_expired_terminal()

    async with clean_database.connect() as connection:
        after = (
            await connection.execute(sa.select(ADMIN_AUDIT_EVENTS))
        ).mappings().all()
        remaining = await connection.scalar(
            sa.select(sa.func.count()).select_from(ACTIVATION_REQUESTS)
        )
    assert report.approved_deleted == 1
    assert remaining == 0
    assert len(before) == 1
    assert after == before


async def test_command_purges_the_real_database_and_prints_counts_only(
    clean_database, postgres_dsn, clock, seed_activation, monkeypatch
) -> None:
    import io
    import json
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    from xiaowei_agent.interfaces import activation_retention

    old = stored_request(
        "approved-cli-old",
        status=ActivationStatus.APPROVED,
        terminal_at=clock() - RETENTION - dt.timedelta(days=1),
    )
    fresh = stored_request(
        "approved-cli-fresh",
        status=ActivationStatus.APPROVED,
        terminal_at=clock() - dt.timedelta(days=1),
        tenant_id="tenant-b",
        environment_id="env-b",
    )
    for request in (old, fresh):
        await seed_activation(request)
    for name in list(os.environ):
        if name.startswith("XIAOWEI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XIAOWEI_ENVIRONMENT_ID", "dev")
    stdout, stderr = io.StringIO(), io.StringIO()

    # main() 自己 asyncio.run，因此放到线程里，避免与本测试的事件循环嵌套。
    code = await asyncio.to_thread(
        activation_retention.main,
        [],
        stdout=stdout,
        stderr=stderr,
        engine_factory=lambda _settings: create_async_engine(postgres_dsn),
        clock=clock,
    )

    assert code == 0, stderr.getvalue()
    assert json.loads(stdout.getvalue()) == {
        "pending_expired": 0,
        "approved_deleted": 1,
        "rejected_deleted": 0,
        "expired_deleted": 0,
    }
    output = stdout.getvalue() + stderr.getvalue()
    for leaked in ("approved-cli", "ou_", "tenant-", "env-", "admin-1"):
        assert leaked not in output
    async with clean_database.connect() as connection:
        remaining = (
            await connection.execute(sa.select(ACTIVATION_REQUESTS.c.request_id))
        ).scalars().all()
    assert remaining == ["approved-cli-fresh"]

"""``ActivationStore`` 的真实 PostgreSQL 共享套件绑定。"""

import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from tests.suites.activation_store import ACTIVATION_STORE_CASES, bind
from tests.suites.activation_store import command as activation_command

from xiaowei_agent.contracts.activation import ActivationStatus
from xiaowei_agent.persistence.activation import (
    ActivationCapacityError,
    activation_subject_digest,
)
from xiaowei_agent.persistence.postgres import PostgresActivationStore
from xiaowei_agent.persistence.schema import ACTIVATION_REQUESTS


@pytest.fixture
def activation_store(clean_database, clock):
    return PostgresActivationStore(engine=clean_database, clock=clock)


bind(globals(), ACTIVATION_STORE_CASES)


def test_bound_fixture_is_the_postgres_implementation(activation_store) -> None:
    assert isinstance(activation_store, PostgresActivationStore)


async def test_concurrent_creates_cannot_cross_the_global_pending_limit(
    clean_database, clock
) -> None:
    now = clock()
    rows = []
    for index in range(1023):
        candidate = activation_command(f"ou_seed_{index}")
        rows.append(
            {
                "request_id": f"seed-{index}",
                "tenant_id": candidate.tenant_id,
                "environment_id": candidate.environment_id,
                "provider": candidate.provider.value,
                "subject_ref": candidate.subject_ref,
                "subject_ref_digest": activation_subject_digest(candidate),
                "source": candidate.source.value,
                "source_event_digest": None,
                "source_chat_digest": None,
                "requested_at": now,
                "expires_at": now + dt.timedelta(hours=24),
                "status": ActivationStatus.PENDING.value,
                "decided_at": None,
                "decided_by": None,
                "approved_role": None,
            }
        )
    async with clean_database.begin() as connection:
        await connection.execute(sa.insert(ACTIVATION_REQUESTS), rows)

    stores = (
        PostgresActivationStore(engine=clean_database, clock=clock),
        PostgresActivationStore(engine=clean_database, clock=clock),
    )
    results = await asyncio.gather(
        stores[0].create_or_reuse(command=activation_command("ou_race_a")),
        stores[1].create_or_reuse(command=activation_command("ou_race_b")),
        return_exceptions=True,
    )

    assert sum(isinstance(item, ActivationCapacityError) for item in results) == 1
    async with clean_database.connect() as connection:
        pending = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(ACTIVATION_REQUESTS)
            .where(ACTIVATION_REQUESTS.c.status == ActivationStatus.PENDING.value)
        )
    assert pending == 1024


async def test_partial_unique_index_arbitrates_concurrent_duplicate_subjects(
    clean_database, postgres_dsn, clock
) -> None:
    """绕过 Store 的全局锁，直接证明唯一索引是最终判重边界。"""
    command = activation_command("ou_same_race")
    now = clock()
    engines = [create_async_engine(postgres_dsn, poolclass=sa.pool.NullPool) for _ in range(2)]
    barrier = asyncio.Barrier(2)

    async def insert(index: int) -> object:
        try:
            async with engines[index].begin() as connection:
                await barrier.wait()
                await connection.execute(
                    sa.insert(ACTIVATION_REQUESTS).values(
                        request_id=f"duplicate-{index}",
                        tenant_id=command.tenant_id,
                        environment_id=command.environment_id,
                        provider=command.provider.value,
                        subject_ref=command.subject_ref,
                        subject_ref_digest=activation_subject_digest(command),
                        source=command.source.value,
                        source_event_digest=None,
                        source_chat_digest=None,
                        requested_at=now,
                        expires_at=now + dt.timedelta(hours=24),
                        status=ActivationStatus.PENDING.value,
                        decided_at=None,
                        decided_by=None,
                        approved_role=None,
                    )
                )
            return "inserted"
        except sa.exc.IntegrityError as error:
            return error

    try:
        results = await asyncio.gather(insert(0), insert(1))
    finally:
        for engine in engines:
            await engine.dispose()

    assert results.count("inserted") == 1
    assert sum(isinstance(item, sa.exc.IntegrityError) for item in results) == 1
    async with clean_database.connect() as connection:
        pending = await connection.scalar(
            sa.select(sa.func.count()).select_from(ACTIVATION_REQUESTS)
        )
    assert pending == 1

"""M7 ChannelStore 契约 —— PostgreSQL 实现绑定与并发/迁移实证。"""

import asyncio

import pytest
from tests.conftest import make_envelope, make_submission
from tests.suites.channel_store import CHANNEL_STORE_CASES, _binding, _subscription, bind

from xiaowei_agent.contracts import ProjectionState
from xiaowei_agent.persistence.channel import ClaimProjectionCommand
from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError
from xiaowei_agent.persistence.postgres import PostgresChannelStore


@pytest.fixture
def channel_store(clock, clean_database):
    return PostgresChannelStore(engine=clean_database, clock=clock)


bind(globals(), CHANNEL_STORE_CASES)


async def test_concurrent_binding_replays_one_database_winner(
    channel_store, store, context, clock
) -> None:
    task = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(idempotency_key="channel-concurrent-binding"),
        )
    )
    command = _binding(
        task.task_id,
        clock(),
        projection=_subscription(task.task_id, clock()),
    )

    results = await asyncio.gather(
        *(channel_store.bind_task(command=command) for _ in range(8))
    )

    assert len({item.binding_id for item in results}) == 1


async def test_concurrent_claim_has_one_winner_and_one_database_fence(
    channel_store, store, context, clock
) -> None:
    task = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(idempotency_key="channel-concurrent-claim"),
        )
    )
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(task.task_id, clock())
    )
    results = await asyncio.gather(
        *(
            channel_store.claim_projection_subscription(
                command=ClaimProjectionCommand(
                    subscription_id=subscription.subscription_id,
                    claim_owner=f"worker-{index}",
                    ttl_seconds=30,
                    expected_state=ProjectionState.PENDING_INITIAL,
                )
            )
            for index in range(8)
        )
    )

    winners = [result for result in results if result.applied]
    assert len(winners) == 1
    assert {result.winner for result in results} == {winners[0].winner}


async def test_rev_0006_downgrade_rejects_channel_data_without_authorization(
    channel_store, store, context, clock, clean_database, alembic_runners
) -> None:
    task = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(idempotency_key="channel-downgrade"),
        )
    )
    await channel_store.bind_task(command=_binding(task.task_id, clock()))
    run_upgrade, run_downgrade = alembic_runners

    with pytest.raises(MigrationSafetyError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0005_step_journal")

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0005_step_journal", True)
        await connection.run_sync(run_upgrade)

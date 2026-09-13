"""一次 task attempt 只能由 application 入口持有一份周期 heartbeat。"""

import asyncio
import datetime as dt

import pytest

from xiaowei_agent.application.task_heartbeat import run_with_task_heartbeat
from xiaowei_agent.contracts import LeaseGrant
from xiaowei_agent.persistence.store import TaskAttemptGrant
from xiaowei_agent.runners.deterministic import LeaseLostError


def _grant() -> TaskAttemptGrant:
    return TaskAttemptGrant(
        lease=LeaseGrant(
            task_id="task-1",
            owner="worker-1",
            expires_at=dt.datetime(2026, 9, 13, tzinfo=dt.UTC),
            fencing_token=7,
        ),
        attempt_number=2,
    )


class _RenewingStore:
    def __init__(self, *, lose: bool = False) -> None:
        self.lose = lose
        self.calls = 0
        self.renewed = asyncio.Event()

    async def renew_lease(self, **kwargs: object) -> LeaseGrant | None:
        del kwargs
        self.calls += 1
        self.renewed.set()
        return None if self.lose else _grant().lease


@pytest.mark.asyncio
async def test_heartbeat_covers_the_whole_operation_and_stops_after_it() -> None:
    store = _RenewingStore()
    tick = asyncio.Event()
    release = asyncio.Event()

    async def sleep(_: float) -> None:
        await tick.wait()
        tick.clear()

    async def operation() -> str:
        await release.wait()
        return "done"

    task = asyncio.create_task(
        run_with_task_heartbeat(
            operation,
            grant=_grant(),
            task_store=store,
            lease_ttl_seconds=60,
            heartbeat_interval_seconds=10,
            sleep=sleep,
        )
    )
    tick.set()
    await store.renewed.wait()
    release.set()

    assert await task == "done"
    assert store.calls == 1


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_the_operation() -> None:
    store = _RenewingStore(lose=True)
    tick = asyncio.Event()
    cancelled = asyncio.Event()

    async def sleep(_: float) -> None:
        await tick.wait()

    async def operation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        run_with_task_heartbeat(
            operation,
            grant=_grant(),
            task_store=store,
            lease_ttl_seconds=60,
            heartbeat_interval_seconds=10,
            sleep=sleep,
        )
    )
    tick.set()

    with pytest.raises(LeaseLostError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_settles_operation_and_heartbeat() -> None:
    store = _RenewingStore()
    operation_cancelled = asyncio.Event()
    heartbeat_cancelled = asyncio.Event()

    async def sleep(_: float) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_cancelled.set()

    async def operation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            operation_cancelled.set()

    task = asyncio.create_task(
        run_with_task_heartbeat(
            operation,
            grant=_grant(),
            task_store=store,
            lease_ttl_seconds=60,
            heartbeat_interval_seconds=10,
            sleep=sleep,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert operation_cancelled.is_set()
    assert heartbeat_cancelled.is_set()


@pytest.mark.asyncio
async def test_heartbeat_interval_must_stay_below_half_the_lease() -> None:
    async def operation() -> None:
        return None

    with pytest.raises(ValueError, match="below half"):
        await run_with_task_heartbeat(
            operation,
            grant=_grant(),
            task_store=_RenewingStore(),
            lease_ttl_seconds=60,
            heartbeat_interval_seconds=30,
        )

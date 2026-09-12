"""覆盖一次完整 task attempt 的唯一周期续租 helper。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, cast

from xiaowei_agent.persistence.store import TaskAttemptGrant
from xiaowei_agent.runners.deterministic import LeaseLostError

_T = TypeVar("_T")


class LeaseRenewalPort(Protocol):
    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> object | None: ...


async def run_with_task_heartbeat(
    operation: Callable[[], Awaitable[_T]],
    *,
    grant: TaskAttemptGrant,
    task_store: LeaseRenewalPort,
    lease_ttl_seconds: int,
    heartbeat_interval_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _T:
    """续租覆盖 ``operation``；失租或取消时先收净两个子任务再传播。"""
    if isinstance(lease_ttl_seconds, bool) or not isinstance(lease_ttl_seconds, int):
        raise TypeError("lease ttl must be an integer")
    if isinstance(heartbeat_interval_seconds, bool) or not isinstance(
        heartbeat_interval_seconds, int | float
    ):
        raise TypeError("heartbeat interval must be numeric")
    if lease_ttl_seconds <= 0 or not (
        0 < heartbeat_interval_seconds < lease_ttl_seconds / 2
    ):
        raise ValueError("heartbeat interval must be below half the lease ttl")

    async def heartbeat() -> None:
        while True:
            await sleep(float(heartbeat_interval_seconds))
            renewed = await task_store.renew_lease(
                task_id=grant.task_id,
                owner=grant.lease.owner,
                fencing_token=grant.fencing_token,
                ttl_seconds=lease_ttl_seconds,
            )
            if renewed is None:
                raise LeaseLostError("task lease heartbeat was lost")

    work: asyncio.Future[_T] = asyncio.ensure_future(operation())
    renewer: asyncio.Task[None] = asyncio.create_task(heartbeat())
    try:
        waiters = {
            cast(asyncio.Future[object], work),
            cast(asyncio.Future[object], renewer),
        }
        done, _ = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED
        )
        if renewer in done:
            failure = renewer.exception()
            if failure is not None:
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
                raise failure
        return await work
    finally:
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer


__all__ = ["LeaseRenewalPort", "run_with_task_heartbeat"]

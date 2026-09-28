"""两个真实 WorkerLoop 经 PostgreSQL 并发领取：每个任务只执行一次。"""

import asyncio
from collections import Counter
from typing import Any

from tests.conftest import make_envelope, make_submission

from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import RequestContext
from xiaowei_agent.persistence.postgres import PostgresTaskStore
from xiaowei_agent.persistence.store import TaskAttemptGrant

_TASKS = 6
_WAIT_SECONDS = 10.0


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


class _Runtime:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    async def execute_task(self, *, grant: TaskAttemptGrant, submission: Any) -> None:
        del submission
        self._executed.append(grant.task_id)
        await asyncio.sleep(0.01)


def _race_on_first_listing(
    store: PostgresTaskStore, barrier: asyncio.Barrier, begun: list[str]
) -> None:
    """两个 worker 都拿到同一份候选列表后才开始领取，保证真的撞上同一批任务。"""
    original_list = store.list_dispatchable_tasks
    original_begin = store.begin_task_attempt
    first = True

    async def list_tasks(**kwargs: Any) -> Any:
        nonlocal first
        candidates = await original_list(**kwargs)
        if first:
            first = False
            await barrier.wait()
        return candidates

    async def begin(**kwargs: Any) -> Any:
        begun.append(kwargs["command"].task_id)
        return await original_begin(**kwargs)

    store.list_dispatchable_tasks = list_tasks  # type: ignore[method-assign]
    store.begin_task_attempt = begin  # type: ignore[method-assign]


async def test_two_workers_race_on_postgres_and_each_task_runs_once(
    independent_stores: Any, clock: Any
) -> None:
    first_store, second_store = independent_stores(2)
    for index in range(_TASKS):
        envelope = make_envelope(request_id=f"r{index}", idempotency_key=f"idem-{index}")
        await first_store.create_task(
            submission=make_submission(_context(), envelope=envelope)
        )

    executed: list[str] = []
    begun: list[str] = []
    barrier = asyncio.Barrier(2)
    settings = Settings(environment_id="dev", worker_max_concurrent_tasks=4)
    workers = []
    for store in (first_store, second_store):
        _race_on_first_listing(store, barrier, begun)
        workers.append(
            WorkerLoop(
                runtime=_Runtime(executed),  # type: ignore[arg-type]
                task_store=store,
                clock=clock,
                monotonic=lambda: 0.0,
                settings=settings,
                sleep=asyncio.sleep,
            )
        )

    stop = asyncio.Event()
    running = [asyncio.create_task(worker.run(stop)) for worker in workers]
    try:
        async with asyncio.timeout(_WAIT_SECONDS):
            while len(set(executed)) < _TASKS:
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.gather(*running)

    # 两边都对同一任务发起过领取，但 CAS 只让一个赢：每个任务恰好执行一次。
    assert any(count > 1 for count in Counter(begun).values())
    assert Counter(executed) == Counter(set(executed))
    assert len(executed) == _TASKS

"""F1-0b PR-A：worker 有界并发、停机宽限与并发下的故障语义。"""

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from tests.conftest import make_envelope, make_submission
from tests.fakes.clock import ManualClock

from xiaowei_agent.application.worker import (
    WorkerInfrastructureExhaustedError,
    WorkerInvariantError,
    WorkerLoop,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    RequestContext,
    TaskLookup,
    TransitionRejection,
)
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.store import DispatchQuery, TaskAttemptGrant
from xiaowei_agent.runners.deterministic import LifecycleError

_NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
_WAIT_SECONDS = 2.0
# 与 heartbeat(10)、poll(1)、backoff 都不同的值，受控 sleep 靠它认出“停机宽限”。
_GRACE_SECONDS = 7.0


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "environment_id": "dev",
        "worker_shutdown_grace_seconds": _GRACE_SECONDS,
    }
    return Settings(**(values | updates))


def _lookup(task_id: str) -> TaskLookup:
    return TaskLookup(task_id=task_id, tenant_id="dev-local", environment_id="dev")


class _Runtime:
    def __init__(self, action: Callable[[TaskAttemptGrant], Awaitable[None]]) -> None:
        self._action = action
        self.grants: list[TaskAttemptGrant] = []
        self.active = 0
        self.max_active = 0

    async def execute_task(self, *, grant: TaskAttemptGrant, submission: Any) -> None:
        del submission
        self.grants.append(grant)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self._action(grant)
        finally:
            self.active -= 1


async def _add_task(store: InMemoryTaskStore, index: int) -> str:
    envelope = make_envelope(request_id=f"r{index}", idempotency_key=f"idem-{index}")
    record = await store.create_task(
        submission=make_submission(_context(), envelope=envelope)
    )
    return record.task_id


async def _store_with_tasks(
    clock: ManualClock, count: int
) -> tuple[InMemoryTaskStore, list[str]]:
    store = InMemoryTaskStore(clock=clock)
    for index in range(count):
        await _add_task(store, index)
    # 按存储真实的派发顺序排列，测试不依赖创建顺序的假设。
    ready = await store.list_dispatchable_tasks(
        query=DispatchQuery(tenant_id="dev-local", environment_id="dev", limit=100)
    )
    return store, [record.task_id for record in ready]


class _Gates:
    """每个任务一扇门：记录进入，等待放行，记录是否被取消。"""

    def __init__(self) -> None:
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.cancelled: set[str] = set()
        self.finished: set[str] = set()

    def _pair(self, task_id: str) -> tuple[asyncio.Event, asyncio.Event]:
        entered = self.entered.setdefault(task_id, asyncio.Event())
        release = self.release.setdefault(task_id, asyncio.Event())
        return entered, release

    async def run(self, grant: TaskAttemptGrant) -> None:
        entered, release = self._pair(grant.task_id)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            self.cancelled.add(grant.task_id)
            raise
        self.finished.add(grant.task_id)

    async def wait_entered(self, task_id: str) -> None:
        await asyncio.wait_for(self._pair(task_id)[0].wait(), _WAIT_SECONDS)

    def open(self, task_id: str) -> None:
        self._pair(task_id)[1].set()


class _Sleep:
    """停机宽限由测试放行；其他 sleep 只让出一下事件循环。"""

    def __init__(self) -> None:
        self.grace_started = asyncio.Event()
        self.grace_elapsed = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        if seconds == _GRACE_SECONDS:
            self.grace_started.set()
            await self.grace_elapsed.wait()
            return
        await asyncio.sleep(0.001)


async def _tiny_sleep(_: float) -> None:
    await asyncio.sleep(0.001)


def _counting_begin(
    store: InMemoryTaskStore, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    claimed: list[str] = []
    original = store.begin_task_attempt

    async def begin(**kwargs: Any) -> Any:
        claimed.append(kwargs["command"].task_id)
        return await original(**kwargs)

    monkeypatch.setattr(store, "begin_task_attempt", begin)
    return claimed


def _worker(
    store: InMemoryTaskStore,
    runtime: _Runtime,
    clock: ManualClock,
    *,
    sleep: Callable[[float], Awaitable[None]] = _tiny_sleep,
    monotonic: Callable[[], float] = lambda: 0.0,
    **settings: object,
) -> WorkerLoop:
    return WorkerLoop(
        runtime=runtime,  # type: ignore[arg-type]
        task_store=store,
        clock=clock,
        monotonic=monotonic,
        settings=_settings(**settings),
        sleep=sleep,
    )


async def _finish(running: "asyncio.Task[None]", stop: asyncio.Event) -> None:
    """用 stop 收尾而不是取消：没按预期结束时 run 会正常返回，断言清楚变红。"""
    await asyncio.wait({running}, timeout=_WAIT_SECONDS)
    stop.set()


# ---- 并发与上限 ----


@pytest.mark.asyncio
async def test_one_long_task_does_not_block_other_tasks_in_a_round() -> None:
    clock = ManualClock(start=_NOW)
    store, (slow, fast) = await _store_with_tasks(clock, 2)
    gates = _Gates()
    gates.open(fast)
    polling = asyncio.create_task(_worker(store, _Runtime(gates.run), clock).poll_once())
    try:
        await gates.wait_entered(slow)
        # 顺序执行时 fast 要等 slow 结束，这里会超时。
        await gates.wait_entered(fast)
    finally:
        gates.open(slow)
    assert await asyncio.wait_for(polling, _WAIT_SECONDS) == 2


@pytest.mark.asyncio
async def test_worker_claims_only_while_a_slot_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, task_ids = await _store_with_tasks(clock, 3)
    claimed = _counting_begin(store, monkeypatch)
    gates = _Gates()
    runtime = _Runtime(gates.run)
    worker = _worker(store, runtime, clock, worker_max_concurrent_tasks=2)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    try:
        await gates.wait_entered(task_ids[0])
        await gates.wait_entered(task_ids[1])
        for _ in range(20):
            await asyncio.sleep(0.002)
        # 两个槽都占着：第三个任务既不领取，也不进入 Runtime。
        assert claimed == task_ids[:2]
        gates.open(task_ids[0])
        await gates.wait_entered(task_ids[2])
        assert claimed == task_ids
        assert runtime.max_active == 2
    finally:
        for task_id in task_ids:
            gates.open(task_id)
        stop.set()
        await asyncio.wait_for(running, _WAIT_SECONDS)


@pytest.mark.asyncio
async def test_run_keeps_claiming_new_tasks_while_a_long_task_runs() -> None:
    clock = ManualClock(start=_NOW)
    store, (slow,) = await _store_with_tasks(clock, 1)
    gates = _Gates()
    stop = asyncio.Event()
    running = asyncio.create_task(_worker(store, _Runtime(gates.run), clock).run(stop))
    try:
        await gates.wait_entered(slow)
        later = await _add_task(store, 1)
        gates.open(later)
        await gates.wait_entered(later)
        assert not gates.release[slow].is_set()
    finally:
        gates.open(slow)
        stop.set()
        await asyncio.wait_for(running, _WAIT_SECONDS)


@pytest.mark.asyncio
async def test_limit_one_keeps_the_previous_one_at_a_time_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, task_ids = await _store_with_tasks(clock, 3)
    claimed = _counting_begin(store, monkeypatch)
    events: list[str] = []

    async def record(grant: TaskAttemptGrant) -> None:
        # 进入时已领取数必须等于已开始数：上一个结束前不领取下一个。
        events.append(f"run:{grant.task_id}:claimed={len(claimed)}")
        await asyncio.sleep(0.002)

    runtime = _Runtime(record)
    first_round = _worker(store, runtime, clock, worker_max_concurrent_tasks=1)
    assert await asyncio.wait_for(first_round.poll_once(), _WAIT_SECONDS) == 1
    stop = asyncio.Event()
    running = asyncio.create_task(
        _worker(store, runtime, clock, worker_max_concurrent_tasks=1).run(stop)
    )
    for _ in range(200):
        if len(runtime.grants) == 3:
            break
        await asyncio.sleep(0.002)
    stop.set()
    await asyncio.wait_for(running, _WAIT_SECONDS)

    assert claimed == task_ids
    assert [grant.task_id for grant in runtime.grants] == task_ids
    assert events == [
        f"run:{task_id}:claimed={index + 1}" for index, task_id in enumerate(task_ids)
    ]
    assert runtime.max_active == 1


# ---- 正常停机：停止领取，宽限内等完，超时才取消 ----


@pytest.mark.asyncio
async def test_stop_stops_claiming_and_waits_for_in_flight_tasks_within_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, task_ids = await _store_with_tasks(clock, 2)
    claimed = _counting_begin(store, monkeypatch)
    gates = _Gates()
    sleep = _Sleep()
    stop = asyncio.Event()
    running = asyncio.create_task(
        _worker(store, _Runtime(gates.run), clock, sleep=sleep).run(stop)
    )
    for task_id in task_ids:
        await gates.wait_entered(task_id)
    stop.set()
    await asyncio.wait_for(sleep.grace_started.wait(), _WAIT_SECONDS)
    await _add_task(store, 9)
    for _ in range(10):
        await asyncio.sleep(0.002)
    assert not running.done()
    # 宽限内放行：在途任务正常完成，不被取消，停机后也不再领取。
    for task_id in task_ids:
        gates.open(task_id)
    await asyncio.wait_for(running, _WAIT_SECONDS)
    assert gates.finished == set(task_ids)
    assert gates.cancelled == set()
    assert claimed == task_ids


@pytest.mark.asyncio
async def test_grace_timeout_cancels_and_the_lease_recovers_the_task() -> None:
    clock = ManualClock(start=_NOW)
    store, (task_id,) = await _store_with_tasks(clock, 1)
    gates = _Gates()
    sleep = _Sleep()
    stop = asyncio.Event()
    before = asyncio.all_tasks()
    running = asyncio.create_task(
        _worker(store, _Runtime(gates.run), clock, sleep=sleep).run(stop)
    )
    await gates.wait_entered(task_id)
    stop.set()
    await asyncio.wait_for(sleep.grace_started.wait(), _WAIT_SECONDS)
    sleep.grace_elapsed.set()
    await asyncio.wait_for(running, _WAIT_SECONDS)
    assert gates.cancelled == {task_id}
    # run 返回后不留后台任务（尝试与续租都已收净）。
    assert {task for task in asyncio.all_tasks() if not task.done()} <= before | {
        asyncio.current_task()
    }

    # 被取消的尝试不写终态；lease 过期后由另一个 worker 按原恢复路径接手。
    stuck = await store.get(lookup=_lookup(task_id))
    assert stuck.attempt_number == 1 and stuck.lease_owner is not None
    successor = _Runtime(gates.run)
    gates.open(task_id)
    assert await _worker(store, successor, clock).poll_once() == 0
    clock.advance(seconds=_settings().lease_ttl_seconds + 1)
    assert await _worker(store, successor, clock).poll_once() == 1
    assert [grant.attempt_number for grant in successor.grants] == [2]


@pytest.mark.asyncio
async def test_fail_stop_cancels_in_flight_tasks_without_waiting_for_grace() -> None:
    clock = ManualClock(start=_NOW)
    store, (slow, broken) = await _store_with_tasks(clock, 2)
    gates = _Gates()
    sleep = _Sleep()

    async def act(grant: TaskAttemptGrant) -> None:
        if grant.task_id == broken:
            await gates.wait_entered(slow)
            raise LifecycleError(
                "transition rejected",
                rejection=TransitionRejection.ILLEGAL_TRANSITION,
            )
        await gates.run(grant)

    stop = asyncio.Event()
    running = asyncio.create_task(
        _worker(store, _Runtime(act), clock, sleep=sleep).run(stop)
    )
    await _finish(running, stop)
    with pytest.raises(WorkerInvariantError):
        await running
    assert gates.cancelled == {slow}
    assert not sleep.grace_started.is_set()


# ---- 并发下的故障语义与原串行一致 ----


def _unavailable() -> PersistenceUnavailableError:
    return PersistenceUnavailableError(category=PersistenceUnavailableCategory.CONNECT)


@pytest.mark.asyncio
async def test_a_fatal_failure_wins_over_a_transient_one_in_the_same_batch() -> None:
    clock = ManualClock(start=_NOW)
    store, (transient, fatal) = await _store_with_tasks(clock, 2)
    both_entered = asyncio.Event()
    arrivals: list[str] = []

    async def act(grant: TaskAttemptGrant) -> None:
        arrivals.append(grant.task_id)
        if len(arrivals) == 2:
            both_entered.set()
        await both_entered.wait()
        if grant.task_id == transient:
            raise _unavailable()
        raise LifecycleError(
            "transition rejected",
            rejection=TransitionRejection.ILLEGAL_TRANSITION,
        )

    stop = asyncio.Event()
    running = asyncio.create_task(_worker(store, _Runtime(act), clock).run(stop))
    await _finish(running, stop)
    # 先登记的临时错误不能把后登记的致命错误吞掉。
    with pytest.raises(WorkerInvariantError):
        await running


@pytest.mark.asyncio
async def test_a_fatal_failure_wins_in_poll_once_too() -> None:
    clock = ManualClock(start=_NOW)
    store, (transient, _) = await _store_with_tasks(clock, 2)
    both_entered = asyncio.Event()
    arrivals: list[str] = []

    async def act(grant: TaskAttemptGrant) -> None:
        arrivals.append(grant.task_id)
        if len(arrivals) == 2:
            both_entered.set()
        await both_entered.wait()
        if grant.task_id == transient:
            raise _unavailable()
        raise LifecycleError(
            "transition rejected",
            rejection=TransitionRejection.ILLEGAL_TRANSITION,
        )

    with pytest.raises(WorkerInvariantError):
        await asyncio.wait_for(
            _worker(store, _Runtime(act), clock).poll_once(), _WAIT_SECONDS
        )


class _Monotonic:
    """poll 与退避推进单调时钟；heartbeat 的 sleep 挂起，不参与计时。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        if seconds == _settings().heartbeat_interval_seconds:
            await asyncio.Event().wait()
        self.value += seconds
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4])
async def test_claiming_alone_does_not_reset_the_infrastructure_window(
    limit: int,
) -> None:
    clock = ManualClock(start=_NOW)
    store, _ = await _store_with_tasks(clock, 16)
    monotonic = _Monotonic()

    async def fail(_: TaskAttemptGrant) -> None:
        raise _unavailable()

    stop = asyncio.Event()
    running = asyncio.create_task(
        _worker(
            store,
            _Runtime(fail),
            clock,
            sleep=monotonic.sleep,
            monotonic=monotonic,
            worker_max_concurrent_tasks=limit,
            infrastructure_backoff_base_seconds=1.0,
            infrastructure_backoff_cap_seconds=1.0,
            continuous_infrastructure_failure_window_seconds=2.5,
        ).run(stop)
    )
    await _finish(running, stop)
    # 每次执行都遇到数据库不可用：领取成功不是“整轮无故障”，窗口必须累计到 fail-stop。
    with pytest.raises(WorkerInfrastructureExhaustedError):
        await running


@pytest.mark.asyncio
async def test_a_clean_completion_still_resets_the_infrastructure_window() -> None:
    clock = ManualClock(start=_NOW)
    store, task_ids = await _store_with_tasks(clock, 6)
    monotonic = _Monotonic()
    healthy = set(task_ids[1::2])

    async def alternate(grant: TaskAttemptGrant) -> None:
        if grant.task_id not in healthy:
            raise _unavailable()

    runtime = _Runtime(alternate)
    stop = asyncio.Event()
    running = asyncio.create_task(
        _worker(
            store,
            runtime,
            clock,
            sleep=monotonic.sleep,
            monotonic=monotonic,
            worker_max_concurrent_tasks=1,
            infrastructure_backoff_base_seconds=1.0,
            infrastructure_backoff_cap_seconds=1.0,
            continuous_infrastructure_failure_window_seconds=1.5,
        ).run(stop)
    )
    for _ in range(500):
        if len(runtime.grants) == len(task_ids):
            break
        await asyncio.sleep(0)
    stop.set()
    # 故障与成功交替：每次成功完成都清空窗口，不会误判为连续故障。
    await asyncio.wait_for(running, _WAIT_SECONDS)
    assert len(runtime.grants) == len(task_ids)

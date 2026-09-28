"""M5 Worker 的调度、重试与 fail-stop 契约。"""

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from tests.conftest import make_envelope, make_submission
from tests.fakes.clock import ManualClock
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.runtime import RetryableTaskError
from xiaowei_agent.application.worker import (
    WorkerInfrastructureExhaustedError,
    WorkerInvariantError,
    WorkerLoop,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelAdvisory,
    ModelUsage,
    RequestContext,
    RetryReason,
    TaskLookup,
    TaskStatus,
    TransitionRejection,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.store import (
    DispatchQuery,
    TaskAttemptGrant,
    TransitionCommand,
)
from xiaowei_agent.runners.runner import WorkflowPaused

_NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {"environment_id": "dev"}
    return Settings(**(values | updates))


class _Runtime:
    def __init__(self, action: Callable[[TaskAttemptGrant], Awaitable[None]]) -> None:
        self._action = action
        self.grants: list[TaskAttemptGrant] = []

    async def execute_task(self, *, grant: TaskAttemptGrant, submission: Any) -> None:
        self.grants.append(grant)
        await self._action(grant)


class _UnsupportedSchemaPlanStore:
    async def save(self, **_: Any) -> None:
        raise AssertionError("resume must not save a new plan")

    async def load(self, *, task_id: str) -> Any:
        from xiaowei_agent.persistence.plans import PlanSchemaVersionUnsupportedError

        raise PlanSchemaVersionUnsupportedError(task_id=task_id)


class _BlockingInteractionModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def classify(self, request: Any) -> InteractionModelResult:
        del request
        self.entered.set()
        await self.release.wait()
        return InteractionModelResult(
            draft=InteractionDraft(
                proposed_kind=InteractionKind.CAPABILITY_REQUEST,
                capability_draft=IntentDraft(
                    intent="starrocks.slow_query.diagnose",
                    slots={"window_minutes": "30"},
                    missing=(),
                    confidence=0.8,
                    source=IntentSource.MODEL,
                ),
                confidence=0.8,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(),
        )


class _BlockingAdvisoryModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_advisory(
        self, request: Any, *, max_output_tokens: int
    ) -> AdvisoryModelResult:
        del request, max_output_tokens
        self.entered.set()
        await self.release.wait()
        return AdvisoryModelResult(
            advisory=ModelAdvisory(
                analysis="合成分析", suggestions=(), uncertainties=()
            ),
            usage=ModelUsage(),
        )


async def _ready_store(clock: ManualClock) -> tuple[InMemoryTaskStore, str]:
    store = InMemoryTaskStore(clock=clock)
    submission = make_submission(_context())
    record = await store.create_task(submission=submission)
    return store, record.task_id


@pytest.mark.asyncio
async def test_worker_claims_and_executes_with_a_process_unique_owner() -> None:
    clock = ManualClock(start=_NOW)
    store, task_id = await _ready_store(clock)

    async def succeed(grant: TaskAttemptGrant) -> None:
        assert grant.task_id == task_id

    first = WorkerLoop(
        runtime=_Runtime(succeed),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )
    second = WorkerLoop(
        runtime=_Runtime(succeed),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )

    assert first.owner != second.owner
    assert await first.poll_once() == 1


def test_worker_constructor_requires_every_explicit_dependency() -> None:
    with pytest.raises(TypeError):
        WorkerLoop()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_retryable_task_failure_schedules_retry_with_the_original_grant() -> None:
    clock = ManualClock(start=_NOW)
    store, task_id = await _ready_store(clock)

    async def fail(grant: TaskAttemptGrant) -> None:
        raise RetryableTaskError(
            task_id=grant.task_id,
            reason=RetryReason.UNCLASSIFIED_ERROR,
        )

    worker = WorkerLoop(
        runtime=_Runtime(fail),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )
    await worker.poll_once()

    winner = await store.get(
        lookup=TaskLookup(task_id=task_id, tenant_id="dev-local", environment_id="dev")
    )
    assert winner.task_failure_count == 1
    assert winner.retry_scheduled_by_attempt == 1
    assert winner.next_attempt_at == _NOW + dt.timedelta(seconds=1)


@pytest.mark.asyncio
async def test_one_worker_heartbeat_covers_a_blocked_runtime_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, _ = await _ready_store(clock)
    runtime_entered = asyncio.Event()
    release_runtime = asyncio.Event()
    tick = asyncio.Event()
    renewed = asyncio.Event()
    renewals = 0
    original_renew = store.renew_lease

    async def block(_: TaskAttemptGrant) -> None:
        runtime_entered.set()
        await release_runtime.wait()

    async def controlled_sleep(_: float) -> None:
        await tick.wait()
        tick.clear()

    async def count_renewal(**kwargs: Any) -> Any:
        nonlocal renewals
        renewals += 1
        result = await original_renew(**kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(store, "renew_lease", count_renewal)
    worker = WorkerLoop(
        runtime=_Runtime(block),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=controlled_sleep,
    )
    polling = asyncio.create_task(worker.poll_once())
    await runtime_entered.wait()
    tick.set()
    await renewed.wait()

    assert renewals == 1
    release_runtime.set()
    assert await polling == 1
    assert renewals == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "runner_renewals"),
    [("intent", 0), ("advisory", 1)],
)
async def test_worker_heartbeat_covers_actual_model_waits_with_one_periodic_owner(
    stage: str,
    runner_renewals: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = (
        _BlockingInteractionModel()
        if stage == "intent"
        else _BlockingAdvisoryModel()
    )
    harness = RuntimeHarness(
        GOLDEN,
        interaction_classifier=model if stage == "intent" else None,
        slow_query_advisory=model if stage == "advisory" else None,
    )
    await harness.runtime.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )
    tick = asyncio.Event()
    renewed = asyncio.Event()
    original_renew = harness.store.renew_lease

    async def controlled_sleep(_: float) -> None:
        await tick.wait()
        tick.clear()

    async def observe_renewal(**kwargs: Any) -> Any:
        result = await original_renew(**kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(harness.store, "renew_lease", observe_renewal)
    worker = WorkerLoop(
        runtime=harness.runtime,
        task_store=harness.store,
        clock=harness.clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=controlled_sleep,
    )
    polling = asyncio.create_task(worker.poll_once())
    await model.entered.wait()

    assert len(harness.store.renewals) == runner_renewals
    renewed.clear()
    tick.set()
    await renewed.wait()
    assert len(harness.store.renewals) == runner_renewals + 1

    model.release.set()
    assert await polling == 1
    # 每条路径最终都是一次 Runner 开工校验 + 本次唯一周期续租。
    assert len(harness.store.renewals) == 2


@pytest.mark.asyncio
async def test_worker_heartbeat_continues_through_retry_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, task_id = await _ready_store(clock)
    scheduling = asyncio.Event()
    release_schedule = asyncio.Event()
    tick = asyncio.Event()
    renewed = asyncio.Event()
    original_schedule = store.schedule_retry
    original_renew = store.renew_lease

    async def fail(grant: TaskAttemptGrant) -> None:
        raise RetryableTaskError(
            task_id=grant.task_id,
            reason=RetryReason.UNCLASSIFIED_ERROR,
        )

    async def block_schedule(*, command: Any) -> Any:
        scheduling.set()
        await release_schedule.wait()
        return await original_schedule(command=command)

    async def controlled_sleep(_: float) -> None:
        await tick.wait()
        tick.clear()

    async def observe_renewal(**kwargs: Any) -> Any:
        result = await original_renew(**kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(store, "schedule_retry", block_schedule)
    monkeypatch.setattr(store, "renew_lease", observe_renewal)
    worker = WorkerLoop(
        runtime=_Runtime(fail),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=controlled_sleep,
    )
    polling = asyncio.create_task(worker.poll_once())
    await scheduling.wait()
    tick.set()
    await renewed.wait()
    release_schedule.set()

    assert await polling == 1
    winner = await store.get(
        lookup=TaskLookup(
            task_id=task_id,
            tenant_id="dev-local",
            environment_id="dev",
        )
    )
    assert winner.retry_scheduled_by_attempt == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["paused", "loser"])
async def test_paused_and_loser_paths_do_not_consume_failure_budget(kind: str) -> None:
    clock = ManualClock(start=_NOW)
    store, task_id = await _ready_store(clock)

    async def stop(grant: TaskAttemptGrant) -> None:
        if kind == "paused":
            raise WorkflowPaused(
                task_id=grant.task_id,
                step_id="s1",
                approval_ref="approval-1",
            )
        from xiaowei_agent.runners.deterministic import LeaseLostError

        raise LeaseLostError("lost execution ownership")

    worker = WorkerLoop(
        runtime=_Runtime(stop),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )
    await worker.poll_once()

    winner = await store.get(
        lookup=TaskLookup(task_id=task_id, tenant_id="dev-local", environment_id="dev")
    )
    assert winner.task_failure_count == 0
    assert winner.retry_scheduled_by_attempt is None


@pytest.mark.asyncio
async def test_worker_fails_stop_on_a_lifecycle_invariant() -> None:
    clock = ManualClock(start=_NOW)
    store, _ = await _ready_store(clock)

    async def fail(_: TaskAttemptGrant) -> None:
        from xiaowei_agent.runners.deterministic import LifecycleError

        raise LifecycleError(
            "transition rejected",
            rejection=TransitionRejection.ILLEGAL_TRANSITION,
        )

    worker = WorkerLoop(
        runtime=_Runtime(fail),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )

    with pytest.raises(WorkerInvariantError):
        await worker.poll_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    [
        TransitionRejection.STALE_FENCING_TOKEN,
        TransitionRejection.LEASE_NOT_HELD,
        TransitionRejection.TERMINAL_PROTECTED,
    ],
)
async def test_worker_stops_cleanly_for_storage_confirmed_losers(
    rejection: TransitionRejection,
) -> None:
    clock = ManualClock(start=_NOW)
    store, _ = await _ready_store(clock)

    async def lose(_: TaskAttemptGrant) -> None:
        from xiaowei_agent.runners.deterministic import LifecycleError

        raise LifecycleError("transition rejected", rejection=rejection)

    worker = WorkerLoop(
        runtime=_Runtime(lose),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 0


@pytest.mark.asyncio
async def test_integrity_failure_is_fail_stop_and_does_not_modify_the_task() -> None:
    clock = ManualClock(start=_NOW)
    store, task_id = await _ready_store(clock)

    async def fail(_: TaskAttemptGrant) -> None:
        raise PersistenceIntegrityError(category=PersistenceIntegrityCategory.SCHEMA)

    worker = WorkerLoop(
        runtime=_Runtime(fail),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )
    before = await store.get(
        lookup=TaskLookup(task_id=task_id, tenant_id="dev-local", environment_id="dev")
    )
    with pytest.raises(PersistenceIntegrityError):
        await worker.poll_once()
    after = await store.get(
        lookup=TaskLookup(task_id=task_id, tenant_id="dev-local", environment_id="dev")
    )
    assert after == before.model_copy(
        update={
            "attempt_number": 1,
            "lease_owner": after.lease_owner,
            "lease_expires_at": after.lease_expires_at,
            "fencing_token": after.fencing_token,
        }
    )


@pytest.mark.asyncio
async def test_unsupported_plan_schema_terminalizes_without_worker_system_failure() -> None:
    harness = RuntimeHarness(GOLDEN)
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    record = await harness.store.get(lookup=harness.lookup)
    await harness.store.transition(
        command=TransitionCommand(
            task_id=view.task_id,
            expected_version=record.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=None,
            terminal_reason=None,
        )
    )
    old_plan_store = _UnsupportedSchemaPlanStore()
    harness.runtime._plans = old_plan_store  # type: ignore[assignment]
    harness.runtime._runner._plans = old_plan_store  # type: ignore[attr-defined]

    worker = WorkerLoop(
        runtime=harness.runtime,
        task_store=harness.store,
        clock=harness.clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1

    winner = await harness.store.get(lookup=harness.lookup)
    assert winner.status is TaskStatus.REJECTED
    assert winner.terminal_reason == "plan.schema_version_unsupported"
    assert harness.gateway.invocations == 0


class _UnavailableBeginStore:
    """读路径恒成功、领取写路径恒不可用的反例。"""

    def __init__(self, inner: InMemoryTaskStore) -> None:
        self._inner = inner

    async def list_dispatchable_tasks(self, **kwargs: Any) -> Any:
        return await self._inner.list_dispatchable_tasks(**kwargs)

    async def begin_task_attempt(self, **_: Any) -> Any:
        raise PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.CONNECT
        )


class _Monotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


@pytest.mark.asyncio
async def test_successful_list_does_not_reset_a_failing_begin_window() -> None:
    clock = ManualClock(start=_NOW)
    store, _ = await _ready_store(clock)
    monotonic = _Monotonic()

    async def unused(_: TaskAttemptGrant) -> None:
        raise AssertionError("runtime must not run without a grant")

    worker = WorkerLoop(
        runtime=_Runtime(unused),
        task_store=_UnavailableBeginStore(store),  # type: ignore[arg-type]
        clock=clock,
        monotonic=monotonic,
        settings=_settings(
            infrastructure_backoff_base_seconds=1.0,
            infrastructure_backoff_cap_seconds=1.0,
            continuous_infrastructure_failure_window_seconds=2.5,
        ),
        sleep=monotonic.sleep,
    )

    with pytest.raises(WorkerInfrastructureExhaustedError):
        await worker.run(asyncio.Event())
    assert monotonic.value == 3.0


# ---- 有界并发：一个长任务不阻塞其他任务，只在有空位时领取 ----

_WAIT_SECONDS = 2.0


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


async def _add_task(store: InMemoryTaskStore, index: int) -> str:
    envelope = make_envelope(request_id=f"r{index}", idempotency_key=f"idem-{index}")
    record = await store.create_task(
        submission=make_submission(_context(), envelope=envelope)
    )
    return record.task_id


class _Gates:
    """每个任务一扇门：记录进入，等待放行。"""

    def __init__(self) -> None:
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.cancelled: set[str] = set()

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

    async def wait_entered(self, task_id: str) -> None:
        await asyncio.wait_for(self._pair(task_id)[0].wait(), _WAIT_SECONDS)

    def open(self, task_id: str) -> None:
        self._pair(task_id)[1].set()


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


@pytest.mark.asyncio
async def test_one_long_task_does_not_block_other_tasks_in_a_round() -> None:
    clock = ManualClock(start=_NOW)
    store, (slow, fast) = await _store_with_tasks(clock, 2)
    gates = _Gates()
    gates.open(fast)
    worker = WorkerLoop(
        runtime=_Runtime(gates.run),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=_tiny_sleep,
    )
    polling = asyncio.create_task(worker.poll_once())
    try:
        await gates.wait_entered(slow)
        # 顺序执行时 fast 要等 slow 结束，这里会超时。
        await gates.wait_entered(fast)
    finally:
        gates.open(slow)
    assert await polling == 2


@pytest.mark.asyncio
async def test_worker_claims_only_while_a_slot_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, task_ids = await _store_with_tasks(clock, 3)
    claimed = _counting_begin(store, monkeypatch)
    gates = _Gates()
    worker = WorkerLoop(
        runtime=_Runtime(gates.run),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(worker_max_concurrent_tasks=2),
        sleep=_tiny_sleep,
    )
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
    finally:
        stop.set()
        await asyncio.wait_for(running, _WAIT_SECONDS)


@pytest.mark.asyncio
async def test_run_keeps_claiming_new_tasks_while_a_long_task_runs() -> None:
    clock = ManualClock(start=_NOW)
    store, (slow,) = await _store_with_tasks(clock, 1)
    gates = _Gates()
    worker = WorkerLoop(
        runtime=_Runtime(gates.run),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=_tiny_sleep,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    try:
        await gates.wait_entered(slow)
        later = await _add_task(store, 1)
        gates.open(later)
        await gates.wait_entered(later)
        assert not gates.release[slow].is_set()
    finally:
        stop.set()
        await asyncio.wait_for(running, _WAIT_SECONDS)


@pytest.mark.asyncio
async def test_stop_ends_claiming_and_cancels_in_flight_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(start=_NOW)
    store, task_ids = await _store_with_tasks(clock, 2)
    claimed = _counting_begin(store, monkeypatch)
    gates = _Gates()
    worker = WorkerLoop(
        runtime=_Runtime(gates.run),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=_tiny_sleep,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    for task_id in task_ids:
        await gates.wait_entered(task_id)
    before = asyncio.all_tasks()
    stop.set()
    await asyncio.wait_for(running, _WAIT_SECONDS)
    # 停机收尾：在跑的尝试被取消并等到退出，run 返回后不留后台任务，也不再领取。
    assert gates.cancelled == set(task_ids)
    assert not {task for task in before if not task.done()} - {
        asyncio.current_task()
    }
    await _add_task(store, 9)
    for _ in range(10):
        await asyncio.sleep(0.002)
    assert claimed == task_ids


@pytest.mark.asyncio
async def test_background_invariant_failure_still_fail_stops_the_worker() -> None:
    clock = ManualClock(start=_NOW)
    store, (slow, broken) = await _store_with_tasks(clock, 2)
    gates = _Gates()

    async def act(grant: TaskAttemptGrant) -> None:
        if grant.task_id == broken:
            from xiaowei_agent.runners.deterministic import LifecycleError

            raise LifecycleError(
                "transition rejected",
                rejection=TransitionRejection.ILLEGAL_TRANSITION,
            )
        await gates.run(grant)

    worker = WorkerLoop(
        runtime=_Runtime(act),
        task_store=store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=_settings(),
        sleep=_tiny_sleep,
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    # 用 stop 收尾而不是取消：没把后台失败上抛时 run 会正常返回，断言清楚变红。
    await asyncio.wait({running}, timeout=_WAIT_SECONDS)
    stop.set()
    with pytest.raises(WorkerInvariantError):
        await running
    # fail-stop 时同进程其他在跑任务被取消，由 lease 过期后恢复。
    assert gates.cancelled == {slow}

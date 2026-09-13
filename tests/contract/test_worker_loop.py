"""M5 Worker 的调度、重试与 fail-stop 契约。"""

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from tests.conftest import make_submission
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
    IntentModelResult,
    IntentSource,
    ModelAdvisory,
    ModelUsage,
    RequestContext,
    RetryReason,
    TaskLookup,
    TransitionRejection,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.persistence.store import TaskAttemptGrant
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


class _BlockingIntentModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_intent(self, request: Any) -> IntentModelResult:
        del request
        self.entered.set()
        await self.release.wait()
        return IntentModelResult(
            draft=IntentDraft(
                intent="starrocks.slow_query.diagnose",
                slots={"window_minutes": "30"},
                missing=(),
                confidence=0.8,
                source=IntentSource.MODEL,
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
    model = _BlockingIntentModel() if stage == "intent" else _BlockingAdvisoryModel()
    harness = RuntimeHarness(
        GOLDEN,
        intent_model=model if stage == "intent" else None,
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

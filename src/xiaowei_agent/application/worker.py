"""Worker 的确定性领取、执行、重试与进程级退避。"""

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, TypeAlias

from xiaowei_agent.application.runtime import RetryableTaskError, XiaoweiRuntime
from xiaowei_agent.contracts import (
    AttemptIntent,
    PipelineStage,
    RetryDecision,
    StageOutcome,
    StepAttemptDecision,
    StepCommitRejection,
    TraceEvent,
    TransitionRejection,
)
from xiaowei_agent.observability.log_sink import (
    StructuredLogTraceSink,
    worker_log_context,
)
from xiaowei_agent.observability.sink import Delivery, delivery_for_write_exception
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.store import (
    Clock,
    DispatchQuery,
    RetryCommand,
    TaskAttemptCommand,
    TaskAttemptGrant,
    TaskStore,
)
from xiaowei_agent.runners.deterministic import LeaseLostError, LifecycleError
from xiaowei_agent.runners.runner import WorkflowPaused

MonotonicClock: TypeAlias = Callable[[], float]
AsyncSleep: TypeAlias = Callable[[float], Coroutine[Any, Any, None]]


class WorkerSettings(Protocol):
    """Worker 消费的最小配置面，避免应用层反向依赖配置适配器。"""

    @property
    def tenant_id(self) -> str: ...

    environment_id: str
    lease_ttl_seconds: int
    infrastructure_backoff_base_seconds: float
    infrastructure_backoff_cap_seconds: float
    continuous_infrastructure_failure_window_seconds: float
    worker_poll_interval_seconds: float
    dispatch_batch_limit: int


class WorkerInfrastructureExhaustedError(RuntimeError):
    """持久化层连续不可用超过进程级容忍窗口。"""


class WorkerInvariantError(RuntimeError):
    """Worker 收到互相矛盾的应用/存储结果，必须 fail-stop。"""


class WorkerSystemFailureError(RuntimeError):
    """系统性持久化故障的脱敏进程级结果。"""


_LOSER_REJECTIONS = frozenset(
    {
        TransitionRejection.STALE_FENCING_TOKEN,
        TransitionRejection.LEASE_NOT_HELD,
        TransitionRejection.TERMINAL_PROTECTED,
        StepAttemptDecision.STALE_FENCING,
        StepAttemptDecision.NOT_RUNNABLE,
        StepCommitRejection.STALE_FENCING,
        StepCommitRejection.NOT_RUNNABLE,
    }
)


class WorkerLoop:
    """只负责任务调度；解释、规划、准入和执行均委托 Runtime。"""

    def __init__(
        self,
        *,
        runtime: XiaoweiRuntime,
        task_store: TaskStore,
        clock: Clock,
        monotonic: MonotonicClock,
        settings: WorkerSettings,
        sleep: AsyncSleep,
    ) -> None:
        self._runtime = runtime
        self._tasks = task_store
        self._clock = clock
        self._monotonic = monotonic
        self._settings = settings
        self._sleep = sleep
        self.owner = f"worker-{uuid.uuid4().hex}"
        self._log = StructuredLogTraceSink(worker_instance=self.owner)

    async def _log_committed(self, events: tuple[TraceEvent, ...]) -> None:
        for event in events:
            await self._log.emit(event, delivery=Delivery.COMMAND_COMMITTED)

    def _retry_event(
        self, *, grant: TaskAttemptGrant, trace_id: str, policy_revision: str | None
    ) -> TraceEvent:
        return TraceEvent(
            event_id=str(uuid.uuid4()),
            trace_id=trace_id,
            task_id=grant.task_id,
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.FAILED,
            occurred_at=self._clock(),
            capability_id=None,
            step_id=None,
            policy_revision=policy_revision,
            attempt_number=grant.attempt_number,
            error=None,
            detail={},
        )

    async def _schedule_retry(
        self,
        *,
        error: RetryableTaskError,
        grant: TaskAttemptGrant,
        trace_id: str,
        policy_revision: str | None,
    ) -> None:
        if error.task_id != grant.task_id:
            raise WorkerInvariantError("retry error belongs to a different task")
        event = self._retry_event(
            grant=grant,
            trace_id=trace_id,
            policy_revision=policy_revision,
        )
        command = RetryCommand(
            grant=grant,
            next_attempt_at=self._clock()
            + dt.timedelta(seconds=self._settings.worker_poll_interval_seconds),
            reason=error.reason,
            audit_events=(event,),
        )
        try:
            result = await self._tasks.schedule_retry(command=command)
        except Exception as exc:
            await self._log.emit(event, delivery=delivery_for_write_exception(exc))
            raise
        delivery = (
            Delivery.COMMAND_COMMITTED
            if result.decision is RetryDecision.SCHEDULED
            else Delivery.COMMAND_ROLLED_BACK
        )
        await self._log.emit(event, delivery=delivery)
        if result.decision is RetryDecision.COMMAND_MISMATCH:
            raise WorkerInvariantError("retry replay disagrees with stored command")

    async def _poll_once(self) -> int:
        candidates = await self._tasks.list_dispatchable_tasks(
            query=DispatchQuery(
                tenant_id=self._settings.tenant_id,
                environment_id=self._settings.environment_id,
                limit=self._settings.dispatch_batch_limit,
            )
        )
        executed = 0
        for candidate in candidates:
            trace_id = uuid.uuid4().hex
            attempt = await self._tasks.begin_task_attempt(
                command=TaskAttemptCommand(
                    task_id=candidate.task_id,
                    intent=AttemptIntent.DISPATCH,
                    owner=self.owner,
                    ttl_seconds=self._settings.lease_ttl_seconds,
                    trace_id=trace_id,
                )
            )
            await self._log_committed(attempt.committed_audit_events)
            if not attempt.applied:
                continue
            grant = attempt.grant
            submission = attempt.submission
            if grant is None or submission is None:
                raise WorkerInvariantError("successful attempt result is incomplete")
            try:
                await self._runtime.execute_task(grant=grant, submission=submission)
            except WorkflowPaused:
                continue
            except LeaseLostError:
                continue
            except LifecycleError as exc:
                if exc.rejection in _LOSER_REJECTIONS:
                    continue
                raise WorkerInvariantError(
                    "worker encountered a lifecycle invariant failure"
                ) from None
            except RetryableTaskError as exc:
                await self._schedule_retry(
                    error=exc,
                    grant=grant,
                    trace_id=trace_id,
                    policy_revision=submission.context.policy_revision,
                )
            executed += 1
        return executed

    async def poll_once(self) -> int:
        """完整执行一轮；任一基础设施异常向上抛，由进程循环统一计时。"""
        with worker_log_context(self.owner):
            return await self._poll_once()

    async def _wait_or_stop(self, seconds: float, stop: asyncio.Event) -> bool:
        sleeper = asyncio.create_task(self._sleep(seconds))
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopper in done:
                sleeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleeper
                return True
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            await sleeper
            return False
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()

    async def _poll_or_stop(self, stop: asyncio.Event) -> tuple[bool, int]:
        poller = asyncio.create_task(self.poll_once())
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {poller, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopper in done:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller
                return True, 0
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            return False, await poller
        finally:
            for task in (poller, stopper):
                if not task.done():
                    task.cancel()

    async def run(self, stop: asyncio.Event) -> None:
        """运行到停止信号，或连续基础设施故障达到 fail-stop 窗口。"""
        first_failure_at: float | None = None
        consecutive_failures = 0
        while not stop.is_set():
            try:
                stopped, _ = await self._poll_or_stop(stop)
            except PersistenceUnavailableError:
                now = self._monotonic()
                if first_failure_at is None:
                    first_failure_at = now
                if (
                    now - first_failure_at
                    >= self._settings.continuous_infrastructure_failure_window_seconds
                ):
                    raise WorkerInfrastructureExhaustedError(
                        "continuous persistence failure window exhausted"
                    ) from None
                delay = min(
                    self._settings.infrastructure_backoff_cap_seconds,
                    self._settings.infrastructure_backoff_base_seconds
                    * (2**consecutive_failures),
                )
                consecutive_failures += 1
                if await self._wait_or_stop(delay, stop):
                    return
                continue
            except PersistenceIntegrityError:
                raise WorkerSystemFailureError(
                    "worker encountered a systemic failure"
                ) from None
            if stopped:
                return
            # 只有整轮没有基础设施异常，才清空窗口和退避计数。
            first_failure_at = None
            consecutive_failures = 0
            if await self._wait_or_stop(
                self._settings.worker_poll_interval_seconds, stop
            ):
                return

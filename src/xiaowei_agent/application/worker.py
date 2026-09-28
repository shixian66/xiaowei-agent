"""Worker 的确定性领取、有界并发执行、重试与进程级退避。"""

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Callable, Collection, Coroutine, Iterable
from functools import partial
from typing import Any, Protocol, TypeAlias

from xiaowei_agent.application.runtime import RetryableTaskError, XiaoweiRuntime
from xiaowei_agent.application.task_heartbeat import run_with_task_heartbeat
from xiaowei_agent.contracts import (
    AttemptIntent,
    PipelineStage,
    RetryDecision,
    StageOutcome,
    StepAttemptDecision,
    StepCommitRejection,
    TaskSubmission,
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
    heartbeat_interval_seconds: float
    infrastructure_backoff_base_seconds: float
    infrastructure_backoff_cap_seconds: float
    continuous_infrastructure_failure_window_seconds: float
    worker_poll_interval_seconds: float
    dispatch_batch_limit: int
    worker_max_concurrent_tasks: int
    worker_shutdown_grace_seconds: float


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
    """只负责任务调度；解释、规划、准入和执行均委托 Runtime。

    最多同时处理 ``worker_max_concurrent_tasks`` 个任务：只在有空位时领取，
    每个领到的尝试在自己的 heartbeat scope 里后台执行，长任务不阻塞其他任务。
    """

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
        # 按领取顺序保存在跑的尝试；dict 保序，同类失败按最早登记者上抛。
        self._in_flight: dict[asyncio.Task[bool], None] = {}

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

    async def _execute_granted_attempt(
        self,
        *,
        grant: TaskAttemptGrant,
        submission: TaskSubmission,
        trace_id: str,
    ) -> bool:
        """执行并在同一 heartbeat scope 内安排 retry。"""
        try:
            await self._runtime.execute_task(grant=grant, submission=submission)
        except WorkflowPaused:
            return False
        except LeaseLostError:
            return False
        except LifecycleError as exc:
            if exc.rejection in _LOSER_REJECTIONS:
                return False
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
        return True

    async def _run_attempt(
        self,
        *,
        grant: TaskAttemptGrant,
        submission: TaskSubmission,
        trace_id: str,
    ) -> bool:
        """在一个 heartbeat scope 内执行已领取的尝试；失租按输家处理。"""
        try:
            return await run_with_task_heartbeat(
                partial(
                    self._execute_granted_attempt,
                    grant=grant,
                    submission=submission,
                    trace_id=trace_id,
                ),
                grant=grant,
                task_store=self._tasks,
                lease_ttl_seconds=self._settings.lease_ttl_seconds,
                heartbeat_interval_seconds=self._settings.heartbeat_interval_seconds,
                sleep=self._sleep,
            )
        except LeaseLostError:
            return False

    async def _dispatch(self) -> list[asyncio.Task[bool]]:
        """只在有空位时列出并领取任务；领到的尝试在后台并发执行。"""
        free = self._settings.worker_max_concurrent_tasks - len(self._in_flight)
        if free <= 0:
            return []
        candidates = await self._tasks.list_dispatchable_tasks(
            query=DispatchQuery(
                tenant_id=self._settings.tenant_id,
                environment_id=self._settings.environment_id,
                limit=min(self._settings.dispatch_batch_limit, free),
            )
        )
        started: list[asyncio.Task[bool]] = []
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
            task = asyncio.create_task(
                self._run_attempt(
                    grant=grant, submission=submission, trace_id=trace_id
                )
            )
            self._in_flight[task] = None
            started.append(task)
        return started

    def _reap(self) -> list[bool]:
        """收走全部已结束的尝试并按原串行语义分类上抛。

        一批里可能同时有多个失败：致命错误（不变量、完整性等）优先于临时
        ``PersistenceUnavailableError``，不能因为临时错误先登记就被丢掉。
        """
        results: list[bool] = []
        fatal: BaseException | None = None
        transient: PersistenceUnavailableError | None = None
        for task in [task for task in self._in_flight if task.done()]:
            del self._in_flight[task]
            if task.cancelled():
                continue
            error = task.exception()
            if error is None:
                results.append(task.result())
            elif isinstance(error, PersistenceUnavailableError):
                transient = transient or error
            elif fatal is None:
                fatal = error
        if fatal is not None:
            raise fatal
        if transient is not None:
            raise transient
        return results

    async def _cancel(self, tasks: Iterable[asyncio.Task[bool]]) -> None:
        """取消并等到尝试真正退出；续租子任务随 heartbeat scope 一起收净。"""
        owned = list(tasks)
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        for task in owned:
            self._in_flight.pop(task, None)

    async def poll_once(self) -> int:
        """领取一轮并等本轮尝试全部结束；任一异常取消其余尝试后向上抛。"""
        with worker_log_context(self.owner):
            started = await self._dispatch()
            try:
                if started:
                    await asyncio.wait(started, return_when=asyncio.FIRST_EXCEPTION)
                return sum(self._reap())
            finally:
                await self._cancel(started)

    async def _dispatch_logged(self) -> None:
        with worker_log_context(self.owner):
            await self._dispatch()

    async def _wait_or_stop(
        self,
        seconds: float,
        stop: asyncio.Event,
        *,
        wake_on: Collection[asyncio.Task[bool]] = (),
    ) -> bool:
        """等满时长、停止信号，或任一在跑尝试结束（空出槽位）。"""
        sleeper = asyncio.create_task(self._sleep(seconds))
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {sleeper, stopper, *wake_on}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopper in done:
                sleeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleeper
                return True
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            if sleeper in done:
                await sleeper
            else:
                sleeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleeper
            return False
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()

    async def _dispatch_or_stop(self, stop: asyncio.Event) -> bool:
        dispatcher = asyncio.create_task(self._dispatch_logged())
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {dispatcher, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopper in done:
                dispatcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatcher
                return True
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            await dispatcher
            return False
        finally:
            for task in (dispatcher, stopper):
                if not task.done():
                    task.cancel()

    async def run(self, stop: asyncio.Event) -> None:
        """运行到停止信号，或连续基础设施故障达到 fail-stop 窗口。

        正常停机：停止领取，宽限内等在途尝试结束，超时才取消。fail-stop 或外部
        取消：立即取消。被取消的尝试由 lease 过期后的既有恢复路径接手。
        """
        try:
            await self._run(stop)
            await self._drain()
        finally:
            await self._cancel(self._in_flight)

    async def _drain(self) -> None:
        """正常停机收尾：已停止领取，只在 ``worker_shutdown_grace_seconds`` 内等待。"""
        grace = asyncio.create_task(
            self._sleep(self._settings.worker_shutdown_grace_seconds)
        )
        try:
            while not grace.done():
                pending = {task for task in self._in_flight if not task.done()}
                if not pending:
                    break
                await asyncio.wait(
                    {grace, *pending}, return_when=asyncio.FIRST_COMPLETED
                )
        finally:
            grace.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grace
        try:
            self._reap()
        except PersistenceUnavailableError:
            # 停机中不再退避重试：没写成的结果交给 lease 过期恢复，与被取消的尝试同路。
            return
        except PersistenceIntegrityError:
            raise WorkerSystemFailureError(
                "worker encountered a systemic failure"
            ) from None

    async def _run(self, stop: asyncio.Event) -> None:
        first_failure_at: float | None = None
        consecutive_failures = 0
        while not stop.is_set():
            try:
                # 先收已结束的尝试：后台失败与领取失败走同一套退避与 fail-stop。
                completed = self._reap()
                stopped = await self._dispatch_or_stop(stop)
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
            # 只有整轮没有基础设施异常，才清空窗口和退避计数。并发下“整轮”指：
            # 本轮有尝试干净结束，或没有尝试在跑。仅领取成功不算——刚启动的尝试
            # 结果未知，执行阶段持续的故障必须能累计到 fail-stop。
            if completed or not self._in_flight:
                first_failure_at = None
                consecutive_failures = 0
            if await self._wait_or_stop(
                self._settings.worker_poll_interval_seconds,
                stop,
                wake_on=tuple(self._in_flight),
            ):
                return

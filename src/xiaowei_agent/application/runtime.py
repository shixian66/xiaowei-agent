"""``XiaoweiRuntime``：应用层唯一编排入口。

一次请求的完整链路：

```text
RequestEnvelope
  → IntentInterpreter.interpret()   → IntentDraft        [INTENT]
  → CapabilityResolver.resolve()    → CandidateSet       [RESOLVER]
  → TargetResolver.resolve()        → ResolvedTarget
  → PlanCompiler.compile_plan()     → ExecutionPlan      [PLANNER]
  → DeterministicStepRunner.start() → TaskOutcome        [ADMISSION/GATEWAY/EVIDENCE]
  → EvidenceLedger.load()           ← 按 evidence_refs 读回
  → Answerability.assess()          → AnswerabilityVerdict [REFLECTION]
  → 依 verdict 确定性判定终态并写 TaskStore                 [LIFECYCLE]
  → RenderPayload                                        [RENDERING]
```

三条承重边界：

1. **Runtime 只调 Runner 的 ``start`` / ``resume``。** 它不构造 ``ToolCall``、不签发
   凭证、不直接调 Gateway——这些由 AST 断言封死。绕过 port 直接摸 Runner 内部是
   "Runtime 读 Runner 内部变量"那类耦合的入口。
2. **证据按引用从 ledger 读回，不从 Runner 拿内容。** ``TaskOutcome`` 只有
   ``evidence_refs``；清空 ledger 后同一次 outcome 必须渲染成"无证据"。
3. **终态由 Runtime 写，且只写一次。** Reflection 只给建议，
   ``terminal_status_for`` 是那一步确定性映射，TaskStore 的终态保护兜底。
"""

import datetime as _dt
import uuid
from enum import StrEnum
from typing import Final

from xiaowei_agent.capabilities.effect import SpecResolutionError
from xiaowei_agent.capabilities.intent import IntentInterpreter
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import OP_LIST, SLOW_QUERY_SURFACE
from xiaowei_agent.capabilities.target import TargetResolutionError, resolve_target
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    AttemptIntent,
    Candidate,
    CapabilitySnapshot,
    EvidenceEnvelope,
    ExecutionPlan,
    IntentDraft,
    PipelineStage,
    RenderPayload,
    RequestContext,
    RequestEnvelope,
    ResolvedTarget,
    RetryReason,
    StageOutcome,
    TaskLookup,
    TaskOutcome,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TaskView,
    TraceEvent,
    task_query_path,
)
from xiaowei_agent.governance.binding import BindingError
from xiaowei_agent.governance.policy import PolicyDeniedError
from xiaowei_agent.governance.sqlguard import SqlGuardError
from xiaowei_agent.observability.sink import (
    Delivery,
    TraceSink,
    delivery_for_write_exception,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.plans import PlanConflictError, PlanNotFoundError
from xiaowei_agent.persistence.store import (
    Clock,
    TaskAttemptCommand,
    TaskAttemptGrant,
    TaskIdCarryingError,
    TaskStore,
    TransitionCommand,
)
from xiaowei_agent.planning.starrocks.compiler import compile_plan
from xiaowei_agent.planning.starrocks.params import (
    DEFAULT_MIN_QUERY_TIME_MS,
    DEFAULT_ROW_LIMIT,
    DEFAULT_WINDOW_MINUTES,
    SlowQueryParams,
    normalise_window,
)
from xiaowei_agent.reflection.answerability import assess, terminal_status_for
from xiaowei_agent.rendering.slow_query import render, render_pending
from xiaowei_agent.runners.deterministic import (
    RECOVERY_DRIFT_REASON,
    DriftError,
    LifecycleError,
    StepJournalInvariantError,
)
from xiaowei_agent.runners.runner import WorkflowPaused, WorkflowRunner

RENDER_REF: Final[None] = None
"""M3 不持久化 ``RenderPayload``。

它只由兼容的同步 ``handle()`` 返回，不另建 render store。M5 的异步查询从 TaskRecord
与 Evidence 纯函数重建终态投影；非终态统一返回 ``render=None``。
"""

_TERMINAL: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELED,
        TaskStatus.INDETERMINATE,
    }
)


class RequestRejectedError(RuntimeError):
    """请求在取数之前就被确定性地拒绝（无候选能力、目标不可解析、参数越界）。

    ``stage`` 放结构化属性：它是错误归因的落点，不是给人读的文本。
    """

    def __init__(self, message: str, *, stage: PipelineStage) -> None:
        super().__init__(message)
        self.stage = stage


class TaskInProgressError(TaskIdCarryingError, RuntimeError):
    """同步入口遇到已经在推进的幂等任务。"""

    def __init__(self, *, task_id: str) -> None:
        super().__init__("task is already in progress", task_id=task_id)


class RetryableTaskError(TaskIdCarryingError, RuntimeError):
    """一次任务尝试遇到未分类故障；只向 Worker 暴露闭集原因。"""

    def __init__(self, *, task_id: str, reason: RetryReason) -> None:
        if not isinstance(reason, RetryReason):
            raise TypeError("reason must be a RetryReason")
        super().__init__("task attempt requires retry", task_id=task_id)
        self.reason = reason


class ApplicationFailure(StrEnum):
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


def classify_application_exception(exc: Exception) -> ApplicationFailure:
    """把应用边界异常收敛为入口可消费的闭集，不暴露存储层类型。"""
    from xiaowei_agent.persistence.store import (
        IdempotencyConflictError,
        TaskNotFoundError,
    )

    if isinstance(exc, IdempotencyConflictError):
        return ApplicationFailure.CONFLICT
    if isinstance(exc, TaskNotFoundError):
        return ApplicationFailure.NOT_FOUND
    if isinstance(exc, PersistenceUnavailableError):
        return ApplicationFailure.UNAVAILABLE
    return ApplicationFailure.INTERNAL


class XiaoweiRuntime:
    """组装依赖、驱动一次请求、产出可审计回答。"""

    def __init__(
        self,
        *,
        interpreter: IntentInterpreter,
        resolver: DeterministicCapabilityResolver,
        snapshot: CapabilitySnapshot,
        task_store: TaskStore,
        ledger: EvidenceLedger,
        runner: WorkflowRunner,
        sink: TraceSink,
        clock: Clock,
    ) -> None:
        self._interpreter = interpreter
        self._resolver = resolver
        self._snapshot = snapshot
        self._tasks = task_store
        self._ledger = ledger
        self._runner = runner
        self._sink = sink
        self._clock = clock

    # --- trace --------------------------------------------------------------

    def _event(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> TraceEvent:
        return TraceEvent(
            event_id=str(uuid.uuid4()),
            trace_id=context.trace_id,
            task_id=task_id,
            stage=stage,
            outcome=outcome,
            occurred_at=self._clock(),
            capability_id=None,
            step_id=None,
            policy_revision=context.policy_revision,
            attempt_number=attempt_number,
            error=None,
            # 与 Runner 同理：阶段与结果已足以归因，detail 是最容易把外部文本
            # 带出去的地方。
            detail={},
        )

    async def _emit(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
        delivery: Delivery,
    ) -> None:
        await self._sink.emit(
            self._event(
                stage=stage,
                outcome=outcome,
                context=context,
                task_id=task_id,
                attempt_number=attempt_number,
            ),
            delivery=delivery,
        )

    # --- 请求处理 -----------------------------------------------------------

    async def submit_task(self, *, submission: TaskSubmission) -> TaskView:
        """只持久化提交事实并返回应用层投影，不解释或执行。"""
        record = await self._tasks.create_task(submission=submission)
        return await self._task_view(record=record)

    async def query_task(self, *, lookup: TaskLookup) -> TaskView:
        """按受信 scope 纯读任务；不发 trace、不改变任何任务事实。"""
        record = await self._tasks.get(lookup=lookup)
        return await self._task_view(record=record)

    async def execute_task(
        self, *, grant: TaskAttemptGrant, submission: TaskSubmission
    ) -> TaskOutcome:
        """在调度层签发的 grant 下重建并执行一次任务尝试。"""
        context = submission.context
        record = await self._tasks.get(
            lookup=TaskLookup(
                task_id=grant.task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        terminal: TaskOutcome | None = None
        retryable = False
        try:
            draft = await self._interpret(
                envelope=submission.envelope,
                context=context,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
            )
            candidate = await self._resolve(
                draft=draft,
                context=context,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
            )
            target, params = await self._plan_inputs(
                draft=draft,
                context=context,
                as_of=submission.as_of,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
            )
            plan = await self._compile(
                candidate=candidate,
                params=params,
                target=target,
                context=context,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
            )
            if record.status is TaskStatus.CREATED:
                terminal = await self._runner.start(
                    grant, plan=plan, target=target, context=context
                )
            elif record.status in {TaskStatus.PLANNING, TaskStatus.RUNNING}:
                terminal = await self._runner.resume(
                    grant, plan=plan, context=context, target=target
                )
            else:
                raise LifecycleError("task status cannot be executed")
        except (WorkflowPaused, LifecycleError):
            raise
        except (PersistenceUnavailableError, PersistenceIntegrityError):
            raise
        except (DriftError, PlanConflictError, PlanNotFoundError, StepJournalInvariantError):
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.FAILED,
                terminal_reason=RECOVERY_DRIFT_REASON,
            )
        except LookupError:
            # gateway adapter 缺失是装配故障，不能写坏当前任务。
            raise
        except (
            RequestRejectedError,
            TargetResolutionError,
            PolicyDeniedError,
            SqlGuardError,
            BindingError,
            SpecResolutionError,
            PermissionError,
        ):
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.REJECTED,
                terminal_reason=None,
            )
        except Exception:
            retryable = True

        if retryable:
            # 必须在 except 块外抛出，避免原始异常留在 __context__。
            raise RetryableTaskError(
                task_id=grant.task_id,
                reason=RetryReason.UNCLASSIFIED_ERROR,
            )
        if terminal is None:
            raise RuntimeError("execution produced no terminal outcome")
        winner, evidences, _ = await self._finalize(
            record=record,
            outcome=terminal,
            context=context,
            grant=grant,
        )
        return TaskOutcome(
            task_id=winner.task_id,
            status=winner.status,
            terminal_reason=winner.terminal_reason,
            evidence_refs=tuple(item.evidence_id for item in evidences),
            render_ref=None,
        )

    async def handle(
        self, *, envelope: RequestEnvelope, context: RequestContext, as_of: _dt.datetime
    ) -> RenderPayload:
        """处理一次请求，返回渲染投影。

        :param as_of: 注入的"现在"；时间窗由它确定性推导，不读进程时钟。
        :raises RequestRejectedError: 取数之前的确定性拒绝。
        """
        draft = await self._interpret(envelope=envelope, context=context)
        candidate = await self._resolve(draft=draft, context=context)
        target, params = await self._plan_inputs(
            draft=draft, context=context, as_of=as_of
        )
        plan = await self._compile(
            candidate=candidate, params=params, target=target, context=context
        )
        record = await self._tasks.create_task(
            submission=TaskSubmission(envelope=envelope, context=context, as_of=as_of)
        )
        if record.status in _TERMINAL:
            # 幂等：同一 idempotency_key 只产生一个任务事实。重复请求不再执行，
            # 而是按**已经落库的**证据与终态重新投影——重跑会既违反 at-most-once，
            # 也会让第二次的回答与第一次不同。
            return await self._render_recorded(record=record, context=context)
        if record.status is not TaskStatus.CREATED or record.attempt_number > 0:
            # ``create_task`` 不返回 created 标记；只有 CREATED/attempt=0 与全新行
            # 无法区分。任何已推进事实都必须让给 Worker，不能由同步入口接管。
            raise TaskInProgressError(task_id=record.task_id)
        attempt = await self._tasks.begin_task_attempt(
            command=TaskAttemptCommand(
                task_id=record.task_id,
                intent=AttemptIntent.DISPATCH,
                owner="runtime-handle",
                ttl_seconds=60,
                trace_id=context.trace_id,
            )
        )
        if attempt.grant is None:
            if attempt.winner.status in _TERMINAL:
                return await self._render_recorded(
                    record=attempt.winner, context=context
                )
            raise TaskInProgressError(task_id=record.task_id)
        try:
            outcome = await self._runner.start(
                attempt.grant, plan=plan, target=target, context=context
            )
        except WorkflowPaused as paused:
            evidences = await self._ledger.load(task_id=record.task_id)
            await self._emit(
                stage=PipelineStage.RENDERING,
                outcome=StageOutcome.OK,
                context=context,
                task_id=record.task_id,
                attempt_number=attempt.grant.attempt_number,
                delivery=Delivery.LOG_AND_DURABLE,
            )
            return render_pending(
                approval_ref=paused.approval_ref, evidences=evidences
            )
        return await self._finish(
            record=record,
            outcome=outcome,
            context=context,
            grant=attempt.grant,
        )

    async def _task_view(self, *, record: TaskRecord) -> TaskView:
        payload: RenderPayload | None = None
        if record.status in _TERMINAL:
            evidences = await self._ledger.load(task_id=record.task_id)
            payload = project_terminal(
                record=record,
                evidences=evidences,
                verdict=assess(evidences=evidences),
            )
        return TaskView(
            task_id=record.task_id,
            status=record.status,
            render=payload,
            query_path=task_query_path(record.task_id),
        )

    async def _render_recorded(
        self, *, record: TaskRecord, context: RequestContext
    ) -> RenderPayload:
        evidences = await self._ledger.load(task_id=record.task_id)
        verdict = assess(evidences=evidences)
        await self._emit(
            stage=PipelineStage.RENDERING,
            outcome=StageOutcome.OK,
            context=context,
            task_id=record.task_id,
            delivery=Delivery.LOG_AND_DURABLE,
        )
        return project_terminal(
            record=record, evidences=evidences, verdict=verdict
        )

    # --- 各阶段 -------------------------------------------------------------

    async def _interpret(
        self,
        *,
        envelope: RequestEnvelope,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> IntentDraft:
        draft = self._interpreter.interpret(text=envelope.text, context=context)
        await self._emit(
            stage=PipelineStage.INTENT,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            attempt_number=attempt_number,
            delivery=_independent_delivery(task_id),
        )
        return draft

    async def _resolve(
        self,
        *,
        draft: IntentDraft,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> Candidate:
        candidates = self._resolver.resolve(
            draft=draft, context=context, snapshot=self._snapshot
        )
        entry = next(
            (item for item in candidates.items if item.operation == OP_LIST), None
        )
        if entry is None:
            await self._emit(
                stage=PipelineStage.RESOLVER,
                outcome=StageOutcome.REJECTED,
                context=context,
                task_id=task_id,
                attempt_number=attempt_number,
                delivery=_independent_delivery(task_id),
            )
            raise RequestRejectedError(
                "no capability candidate for this request",
                stage=PipelineStage.RESOLVER,
            )
        await self._emit(
            stage=PipelineStage.RESOLVER,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            attempt_number=attempt_number,
            delivery=_independent_delivery(task_id),
        )
        return entry

    async def _plan_inputs(
        self,
        *,
        draft: IntentDraft,
        context: RequestContext,
        as_of: _dt.datetime,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> tuple[ResolvedTarget, SlowQueryParams]:
        try:
            target = resolve_target(context=context, draft=draft)
            start, end = normalise_window(
                as_of=as_of, window_minutes=_window_minutes(draft)
            )
            params = SlowQueryParams(
                window_start=start,
                window_end=end,
                min_query_time_ms=DEFAULT_MIN_QUERY_TIME_MS,
                row_limit=DEFAULT_ROW_LIMIT,
                database=draft.slots.get("database"),
                user_name=draft.slots.get("user_name"),
                query_id=draft.slots.get("query_id"),
            )
        except (TargetResolutionError, ValueError) as exc:
            await self._emit(
                stage=PipelineStage.PLANNER,
                outcome=StageOutcome.REJECTED,
                context=context,
                task_id=task_id,
                attempt_number=attempt_number,
                delivery=_independent_delivery(task_id),
            )
            raise RequestRejectedError(
                "request parameters are outside the allowed range",
                stage=PipelineStage.PLANNER,
            ) from exc
        return target, params

    async def _compile(
        self,
        *,
        candidate: Candidate,
        params: SlowQueryParams,
        target: ResolvedTarget,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> ExecutionPlan:
        plan = compile_plan(
            candidate=candidate,
            params=params,
            target=target,
            context=context,
            snapshot=self._snapshot,
            surface=SLOW_QUERY_SURFACE,
        )
        await self._emit(
            stage=PipelineStage.PLANNER,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            attempt_number=attempt_number,
            delivery=_independent_delivery(task_id),
        )
        return plan

    async def _finish(
        self,
        *,
        record: TaskRecord,
        outcome: TaskOutcome,
        context: RequestContext,
        grant: TaskAttemptGrant,
    ) -> RenderPayload:
        """读回证据、判定终态、写 TaskStore、投影。"""
        winner, evidences, verdict = await self._finalize(
            record=record,
            outcome=outcome,
            context=context,
            grant=grant,
        )
        payload = project_terminal(
            record=winner, evidences=evidences, verdict=verdict
        )
        await self._emit(
            stage=PipelineStage.RENDERING,
            outcome=StageOutcome.OK,
            context=context,
            task_id=winner.task_id,
            attempt_number=grant.attempt_number,
            delivery=Delivery.LOG_AND_DURABLE,
        )
        return payload

    async def _finalize(
        self,
        *,
        record: TaskRecord,
        outcome: TaskOutcome,
        context: RequestContext,
        grant: TaskAttemptGrant,
    ) -> tuple[TaskRecord, tuple[EvidenceEnvelope, ...], AnswerabilityVerdict]:
        """在原 grant 下完成终态 CAS，并返回投影所需的持久化事实。"""
        task_id = record.task_id
        if task_id != grant.task_id or outcome.task_id != grant.task_id:
            raise LifecycleError("execution result belongs to a different task")
        # 按引用读回：Runner 的返回值只带 refs，内容一律来自 ledger。
        evidences: tuple[EvidenceEnvelope, ...] = await self._ledger.load(
            task_id=task_id
        )
        verdict = assess(evidences=evidences)
        # REFLECTION 只在**执行本身没出问题、却仍然证据不足**时记为 REJECTED。
        # 上游已经失败（超时、格式错误、预算耗尽）时，证据不足是那次失败的**后果**，
        # 不是第二个根因；两个阶段都标红会让"一次失败指向唯一一个阶段"失效。
        executed_cleanly = outcome.status is TaskStatus.SUCCEEDED
        reflection_event = self._event(
            stage=PipelineStage.REFLECTION,
            outcome=StageOutcome.OK
            if verdict.sufficient or not executed_cleanly
            else StageOutcome.REJECTED,
            context=context,
            task_id=task_id,
            attempt_number=grant.attempt_number,
        )
        status = _terminal_status(outcome=outcome, verdict=verdict)
        current = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        lifecycle_event = self._event(
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            attempt_number=grant.attempt_number,
        )
        try:
            result = await self._tasks.transition(
                command=TransitionCommand(
                    task_id=task_id,
                    expected_version=current.version,
                    to_status=status,
                    fencing_token=grant.fencing_token,
                    terminal_reason=outcome.terminal_reason,
                    audit_events=(reflection_event, lifecycle_event),
                )
            )
        except Exception as exc:
            delivery = delivery_for_write_exception(exc)
            for event in (reflection_event, lifecycle_event):
                await self._sink.emit(event, delivery=delivery)
            raise
        delivery = (
            Delivery.COMMAND_COMMITTED
            if result.applied
            else Delivery.COMMAND_ROLLED_BACK
        )
        for event in (reflection_event, lifecycle_event):
            await self._sink.emit(event, delivery=delivery)
        if not result.applied:
            raise LifecycleError(
                "terminal transition rejected", rejection=result.rejection
            )
        return result.winner, evidences, verdict


def _window_minutes(draft: IntentDraft) -> int:
    """从槽位取时间窗；取不出或非法即用声明默认值。

    槽位来自模型输出，因此这里只接受纯数字字符串——参数层随后还会再校验一次上限。
    """
    raw = draft.slots.get("window_minutes")
    if raw is None or not raw.isdigit():
        return DEFAULT_WINDOW_MINUTES
    return int(raw)


def _independent_delivery(task_id: str | None) -> Delivery:
    return Delivery.LOG_ONLY if task_id is None else Delivery.LOG_AND_DURABLE


def _task_outcome(
    *, task_id: str, status: TaskStatus, terminal_reason: str | None
) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        status=status,
        terminal_reason=terminal_reason,
        evidence_refs=(),
        render_ref=None,
    )


def project_terminal(
    *,
    record: TaskRecord,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
) -> RenderPayload:
    """把终态持久化事实投影为回答；纯函数，不发 trace、不做 I/O。"""
    return render(evidences=evidences, verdict=verdict, status=record.status)


def _terminal_status(
    *, outcome: TaskOutcome, verdict: AnswerabilityVerdict
) -> TaskStatus:
    """把执行层结论与可答性结论合成终态。

    执行层已经判定失败（例如预算耗尽）时，**不因为"证据看起来够"而升级为成功**：
    两个结论取更保守的那个。
    """
    executed = outcome.status
    if executed is not TaskStatus.SUCCEEDED:
        return executed
    suggested = terminal_status_for(verdict)
    if executed is TaskStatus.SUCCEEDED and suggested is TaskStatus.SUCCEEDED:
        return TaskStatus.SUCCEEDED
    return TaskStatus.INDETERMINATE

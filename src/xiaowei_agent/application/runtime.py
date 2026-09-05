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
from typing import Final

from xiaowei_agent.capabilities.intent import IntentInterpreter
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import OP_LIST, SLOW_QUERY_SURFACE
from xiaowei_agent.capabilities.target import TargetResolutionError, resolve_target
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
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
    StageOutcome,
    TaskLookup,
    TaskOutcome,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TraceEvent,
)
from xiaowei_agent.observability.sink import TraceSink
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.store import Clock, TaskStore
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
from xiaowei_agent.runners.runner import WorkflowPaused, WorkflowRunner

RENDER_REF: Final[None] = None
"""M3 不持久化 ``RenderPayload``。

它由 Runtime 同步返回，没有任何读回方。与其造一个没人读的 store，不如把这个决定
显式钉住——M4/M5 拆出 worker 与 API 后才有真实的读回需求。
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
        self._event_seq = 0

    # --- trace --------------------------------------------------------------

    def _emit(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str | None = None,
    ) -> None:
        self._event_seq += 1
        self._sink.emit(
            TraceEvent(
                event_id=f"rt-{self._event_seq}",
                trace_id=context.trace_id,
                task_id=task_id,
                stage=stage,
                outcome=outcome,
                occurred_at=self._clock(),
                capability_id=None,
                step_id=None,
                policy_revision=context.policy_revision,
                error=None,
                # 与 Runner 同理：阶段与结果已足以归因，detail 是最容易把外部文本
                # 带出去的地方。
                detail={},
            )
        )

    # --- 请求处理 -----------------------------------------------------------

    async def handle(
        self, *, envelope: RequestEnvelope, context: RequestContext, as_of: _dt.datetime
    ) -> RenderPayload:
        """处理一次请求，返回渲染投影。

        :param as_of: 注入的"现在"；时间窗由它确定性推导，不读进程时钟。
        :raises RequestRejectedError: 取数之前的确定性拒绝。
        """
        draft = self._interpret(envelope=envelope, context=context)
        candidate = self._resolve(draft=draft, context=context)
        target, params = self._plan_inputs(
            draft=draft, context=context, as_of=as_of
        )
        plan = self._compile(
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
        try:
            outcome = await self._runner.start(
                record.task_id, plan=plan, target=target, context=context
            )
        except WorkflowPaused as paused:
            evidences = await self._ledger.load(task_id=record.task_id)
            self._emit(
                stage=PipelineStage.RENDERING,
                outcome=StageOutcome.OK,
                context=context,
                task_id=record.task_id,
            )
            return render_pending(
                approval_ref=paused.approval_ref, evidences=evidences
            )
        return await self._finish(
            record=record, outcome=outcome, context=context
        )

    async def _render_recorded(
        self, *, record: TaskRecord, context: RequestContext
    ) -> RenderPayload:
        evidences = await self._ledger.load(task_id=record.task_id)
        verdict = assess(evidences=evidences)
        self._emit(
            stage=PipelineStage.RENDERING,
            outcome=StageOutcome.OK,
            context=context,
            task_id=record.task_id,
        )
        return render(evidences=evidences, verdict=verdict, status=record.status)

    # --- 各阶段 -------------------------------------------------------------

    def _interpret(
        self, *, envelope: RequestEnvelope, context: RequestContext
    ) -> IntentDraft:
        draft = self._interpreter.interpret(text=envelope.text, context=context)
        self._emit(
            stage=PipelineStage.INTENT, outcome=StageOutcome.OK, context=context
        )
        return draft

    def _resolve(self, *, draft: IntentDraft, context: RequestContext) -> Candidate:
        candidates = self._resolver.resolve(
            draft=draft, context=context, snapshot=self._snapshot
        )
        entry = next(
            (item for item in candidates.items if item.operation == OP_LIST), None
        )
        if entry is None:
            self._emit(
                stage=PipelineStage.RESOLVER,
                outcome=StageOutcome.REJECTED,
                context=context,
            )
            raise RequestRejectedError(
                "no capability candidate for this request",
                stage=PipelineStage.RESOLVER,
            )
        self._emit(
            stage=PipelineStage.RESOLVER, outcome=StageOutcome.OK, context=context
        )
        return entry

    def _plan_inputs(
        self, *, draft: IntentDraft, context: RequestContext, as_of: _dt.datetime
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
            self._emit(
                stage=PipelineStage.PLANNER,
                outcome=StageOutcome.REJECTED,
                context=context,
            )
            raise RequestRejectedError(
                "request parameters are outside the allowed range",
                stage=PipelineStage.PLANNER,
            ) from exc
        return target, params

    def _compile(
        self,
        *,
        candidate: Candidate,
        params: SlowQueryParams,
        target: ResolvedTarget,
        context: RequestContext,
    ) -> ExecutionPlan:
        plan = compile_plan(
            candidate=candidate,
            params=params,
            target=target,
            context=context,
            snapshot=self._snapshot,
            surface=SLOW_QUERY_SURFACE,
        )
        self._emit(
            stage=PipelineStage.PLANNER, outcome=StageOutcome.OK, context=context
        )
        return plan

    async def _finish(
        self, *, record: TaskRecord, outcome: TaskOutcome, context: RequestContext
    ) -> RenderPayload:
        """读回证据、判定终态、写 TaskStore、投影。"""
        task_id = record.task_id
        # 按引用读回：Runner 的返回值只带 refs，内容一律来自 ledger。
        evidences: tuple[EvidenceEnvelope, ...] = await self._ledger.load(
            task_id=task_id
        )
        verdict = assess(evidences=evidences)
        # REFLECTION 只在**执行本身没出问题、却仍然证据不足**时记为 REJECTED。
        # 上游已经失败（超时、格式错误、预算耗尽）时，证据不足是那次失败的**后果**，
        # 不是第二个根因；两个阶段都标红会让"一次失败指向唯一一个阶段"失效。
        executed_cleanly = outcome.status is TaskStatus.SUCCEEDED
        self._emit(
            stage=PipelineStage.REFLECTION,
            outcome=StageOutcome.OK
            if verdict.sufficient or not executed_cleanly
            else StageOutcome.REJECTED,
            context=context,
            task_id=task_id,
        )
        status = _terminal_status(outcome=outcome, verdict=verdict)
        current = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        if current.status not in _TERMINAL:
            result = await self._tasks.transition(
                task_id=task_id,
                expected_version=current.version,
                to_status=status,
                fencing_token=current.fencing_token,
                terminal_reason=outcome.terminal_reason,
            )
            # 采纳存储层 winner：终态由存储保护，不由调用方自觉。
            status = result.winner.status
        else:
            status = current.status
        self._emit(
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
        )
        payload = render(evidences=evidences, verdict=verdict, status=status)
        self._emit(
            stage=PipelineStage.RENDERING,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
        )
        return payload


def _window_minutes(draft: IntentDraft) -> int:
    """从槽位取时间窗；取不出或非法即用声明默认值。

    槽位来自模型输出，因此这里只接受纯数字字符串——参数层随后还会再校验一次上限。
    """
    raw = draft.slots.get("window_minutes")
    if raw is None or not raw.isdigit():
        return DEFAULT_WINDOW_MINUTES
    return int(raw)


def _terminal_status(
    *, outcome: TaskOutcome, verdict: AnswerabilityVerdict
) -> TaskStatus:
    """把执行层结论与可答性结论合成终态。

    执行层已经判定失败（例如预算耗尽）时，**不因为"证据看起来够"而升级为成功**：
    两个结论取更保守的那个。
    """
    executed = outcome.status
    if executed is TaskStatus.FAILED:
        return TaskStatus.FAILED
    suggested = terminal_status_for(verdict)
    if executed is TaskStatus.SUCCEEDED and suggested is TaskStatus.SUCCEEDED:
        return TaskStatus.SUCCEEDED
    return TaskStatus.INDETERMINATE

"""``XiaoweiRuntime``：应用层唯一编排入口。

一次请求的完整链路：

```text
RequestEnvelope
  → load-or-create interaction artifact                 [MODEL]
  → route_interaction()             → IntentDraft        [INTENT]
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

import asyncio
import datetime as _dt
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from xiaowei_agent.application.capability_input import (
    CapabilityInputBindingError,
    require_exact_params_type,
)
from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingError,
    CapabilityBindingRegistry,
    CapabilityPreparationError,
    CapabilityRuntimeBinding,
    PreparedCapability,
)
from xiaowei_agent.application.interaction_router import (
    InteractionRouteDecision,
    route_interaction,
)
from xiaowei_agent.application.model_advisory import load_or_accept_advisory
from xiaowei_agent.application.model_interaction import load_or_accept_interaction
from xiaowei_agent.application.model_ports import (
    InteractionClassifierPort,
    SlowQueryAdvisoryPort,
)
from xiaowei_agent.application.task_heartbeat import run_with_task_heartbeat
from xiaowei_agent.application.task_view_runtime import (
    TaskViewRuntime,
    assess_evidence,
    project_terminal,
)
from xiaowei_agent.capabilities.effect import SpecResolutionError
from xiaowei_agent.capabilities.intent import IntentInterpreter
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    AnswerabilityVerdict,
    AttemptIntent,
    Candidate,
    CapabilitySnapshot,
    CapabilitySubject,
    ClarificationContext,
    ClarificationReasonCode,
    EvidenceEnvelope,
    ExecutionPlan,
    IntentDraft,
    ModelAdvisory,
    ModelCallObservation,
    ModelFallbackCode,
    ModelInvocationProfile,
    PipelineStage,
    PolicyReason,
    ReadClass,
    RenderPayload,
    RequestContext,
    RequestEnvelope,
    RetryReason,
    RoutingDisposition,
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
    StageOutcome,
    TaskLookup,
    TaskOutcome,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TaskView,
    TraceEvent,
)
from xiaowei_agent.governance.binding import BindingError
from xiaowei_agent.governance.policy import PolicyDeniedError
from xiaowei_agent.governance.sqlguard import SqlGuardError
from xiaowei_agent.observability.sink import (
    Delivery,
    TraceSink,
    delivery_for_write_exception,
)
from xiaowei_agent.persistence.clarification_records import (
    ClarificationRecordCandidate,
    ClarificationRecordConflictError,
    ClarificationRecordGrantError,
    ClarificationRecordStateError,
    ClarificationRecordStore,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.model_artifacts import (
    ModelArtifactConflictError,
    ModelArtifactGrantError,
    ModelArtifactStateError,
    ModelArtifactStore,
)
from xiaowei_agent.persistence.plans import (
    PlanConflictError,
    PlanNotFoundError,
    PlanSchemaVersionUnsupportedError,
    PlanStore,
)
from xiaowei_agent.persistence.store import (
    Clock,
    TaskAttemptCommand,
    TaskAttemptGrant,
    TaskIdCarryingError,
    TaskStore,
    TransitionCommand,
)
from xiaowei_agent.planning.disclosure import DisclosureProjectionError
from xiaowei_agent.planning.slot_verification import (
    SlotVerificationError,
    require_confirmed_projection,
)
from xiaowei_agent.reflection.status import terminal_status_for
from xiaowei_agent.rendering.generic import (
    CONVERSATION_TERMINAL_REASON,
    render_clarification_as_payload,
)
from xiaowei_agent.rendering.pending import render_pending
from xiaowei_agent.runners.deterministic import (
    RECOVERY_DRIFT_REASON,
    DriftError,
    LeaseLostError,
    LifecycleError,
    StepJournalInvariantError,
)
from xiaowei_agent.runners.runner import WorkflowPaused, WorkflowRunner

RENDER_REF: Final[None] = None
"""M3 不持久化 ``RenderPayload``。

它只由兼容的同步 ``handle()`` 返回，不另建 render store。M5 的异步查询从 TaskRecord
与 Evidence 纯函数重建终态投影；非终态统一返回 ``render=None``。
"""


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


class _ClarificationTerminalizedError(Exception):
    """内部控制流：澄清事实已写入，后续交给统一 finalize。"""


class XiaoweiRuntime:
    """组装依赖、驱动一次请求、产出可审计回答。"""

    def __init__(
        self,
        *,
        interpreter: IntentInterpreter,
        resolver: DeterministicCapabilityResolver,
        snapshot: CapabilitySnapshot,
        bindings: CapabilityBindingRegistry,
        task_store: TaskStore,
        plan_store: PlanStore,
        ledger: EvidenceLedger,
        runner: WorkflowRunner,
        sink: TraceSink,
        clock: Clock,
        model_artifacts: ModelArtifactStore,
        model_profile: ModelInvocationProfile,
        clarification_records: ClarificationRecordStore,
        interaction_classifier: InteractionClassifierPort | None = None,
        slow_query_advisory: SlowQueryAdvisoryPort | None = None,
        model_monotonic: Callable[[], float] = time.monotonic,
        lease_ttl_seconds: int = 60,
        heartbeat_interval_seconds: float = 10.0,
        heartbeat_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interpreter = interpreter
        self._resolver = resolver
        self._snapshot = snapshot
        self._bindings = bindings
        self._tasks = task_store
        self._plans = plan_store
        self._ledger = ledger
        self._runner = runner
        self._sink = sink
        self._clock = clock
        self._model_artifacts = model_artifacts
        self._clarification_records = clarification_records
        self._model_profile = model_profile
        self._interaction_classifier = interaction_classifier
        self._slow_query_advisory = slow_query_advisory
        self._model_monotonic = model_monotonic
        self._lease_ttl_seconds = lease_ttl_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_sleep = heartbeat_sleep
        self._task_views = TaskViewRuntime(
            task_store=task_store,
            plan_store=plan_store,
            ledger=ledger,
            bindings=bindings,
            clarification_records=clarification_records,
            model_artifacts=model_artifacts,
            model_profile=model_profile,
        )

    # --- trace --------------------------------------------------------------

    def _event(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
        model: ModelCallObservation | None = None,
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
            model=model,
        )

    async def _emit(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
        model: ModelCallObservation | None = None,
        delivery: Delivery,
    ) -> None:
        await self._sink.emit(
            self._event(
                stage=stage,
                outcome=outcome,
                context=context,
                task_id=task_id,
                attempt_number=attempt_number,
                model=model,
            ),
            delivery=delivery,
        )

    # --- 请求处理 -----------------------------------------------------------

    async def submit_task(self, *, submission: TaskSubmission) -> TaskView:
        """只持久化提交事实并返回应用层投影，不解释或执行。"""
        return await self._task_views.submit_task(submission=submission)

    async def query_task(self, *, lookup: TaskLookup) -> TaskView:
        """按受信 scope 纯读任务；不发 trace、不改变任何任务事实。"""
        return await self._task_views.query_task(lookup=lookup)

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
        binding: CapabilityRuntimeBinding | None = None
        prepared: PreparedCapability | None = None
        recovered_plan: ExecutionPlan | None = None
        retryable = False
        try:
            clarification = await self._load_clarification_context(
                submission=submission
            )
            accepted = await load_or_accept_interaction(
                grant=grant,
                envelope=submission.envelope,
                context=context,
                interpreter=self._interpreter,
                model=self._interaction_classifier,
                profile=self._model_profile,
                artifacts=self._model_artifacts,
                clarification=clarification,
                monotonic=self._model_monotonic,
            )
            if (
                accepted.observation is not None
                and accepted.observation.fallback_code is not ModelFallbackCode.DISABLED
            ):
                fallback = accepted.observation.fallback_code
                await self._emit(
                    stage=PipelineStage.MODEL,
                    outcome=(
                        StageOutcome.OK
                        if fallback is None
                        else StageOutcome.FAILED
                    ),
                    context=context,
                    task_id=grant.task_id,
                    attempt_number=grant.attempt_number,
                    model=accepted.observation,
                    delivery=Delivery.LOG_AND_DURABLE,
                )
            route = route_interaction(draft=accepted.artifact.draft, context=context)
            await self._emit(
                stage=PipelineStage.INTENT,
                outcome=(
                    StageOutcome.OK
                    if route.disposition
                    in {RoutingDisposition.PROCEED, RoutingDisposition.RESPOND}
                    else StageOutcome.REJECTED
                ),
                context=context,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
                delivery=Delivery.LOG_AND_DURABLE,
            )
            if route.intent_draft is None:
                if route.disposition is RoutingDisposition.RESPOND:
                    return await self._complete_conversation(
                        record=record,
                        grant=grant,
                        context=context,
                    )
                if route.disposition is RoutingDisposition.CLARIFY:
                    await self._save_route_clarification(
                        grant=grant,
                        route=route,
                    )
                    terminal = _task_outcome(
                        task_id=grant.task_id,
                        status=TaskStatus.CLARIFICATION_REQUIRED,
                        terminal_reason=None,
                    )
                    raise _ClarificationTerminalizedError
                raise RequestRejectedError(
                    "interaction route rejected",
                    stage=PipelineStage.INTENT,
                )
            draft = route.intent_draft
            candidate, binding = await self._resolve(
                draft=draft,
                context=context,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
            )
            prepared = await self._prepare(
                binding=binding,
                candidate=candidate,
                draft=draft,
                context=context,
                as_of=submission.as_of,
                grant=grant,
                clarification=clarification,
                user_text=submission.envelope.text,
                task_id=grant.task_id,
                attempt_number=grant.attempt_number,
            )
            if prepared is None:
                terminal = _task_outcome(
                    task_id=grant.task_id,
                    status=TaskStatus.CLARIFICATION_REQUIRED,
                    terminal_reason=None,
                )
                raise _ClarificationTerminalizedError
            plan = prepared.plan
            target = prepared.target
            parent_confirmed_slots = (
                None if clarification is None else clarification.confirmed_slots
            )
            if _plan_contains_restricted_read(plan):
                terminal = _task_outcome(
                    task_id=grant.task_id,
                    status=TaskStatus.REJECTED,
                    terminal_reason=PolicyReason.READ_CLASS_NOT_ALLOWED.value,
                )
            elif record.status is TaskStatus.CREATED:
                terminal = await self._runner.start(
                    grant,
                    plan=plan,
                    target=target,
                    context=context,
                    parent_confirmed_slots=parent_confirmed_slots,
                )
            elif record.status in {TaskStatus.PLANNING, TaskStatus.RUNNING}:
                terminal = await self._runner.resume(
                    grant,
                    plan=plan,
                    context=context,
                    target=target,
                    parent_confirmed_slots=parent_confirmed_slots,
                )
            else:
                raise LifecycleError("task status cannot be executed")
        except (WorkflowPaused, LifecycleError):
            raise
        except (PersistenceUnavailableError, PersistenceIntegrityError):
            raise
        except PlanSchemaVersionUnsupportedError as exc:
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.REJECTED,
                terminal_reason=exc.reason_code,
            )
        except DisclosureProjectionError as exc:
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.REJECTED,
                terminal_reason=exc.reason_code,
            )
        except ModelArtifactConflictError:
            # Interaction artifact 在 Resolver 前被复验；若上次尝试已经提交过 plan/证据，
            # 本次必须恢复该 plan 的 binding 才能按事实收成 recovery_drift。否则
            # assess_evidence 会把“有证据但 binding=None”当成另一项不变量错误。
            try:
                stored = await self._plans.load(task_id=grant.task_id)
            except PlanNotFoundError:
                pass
            else:
                recovered_plan = stored.plan
                binding = self._bindings.runtime_for_plan(plan=stored.plan)
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.FAILED,
                terminal_reason=RECOVERY_DRIFT_REASON,
            )
        except (
            DriftError,
            PlanConflictError,
            PlanNotFoundError,
            StepJournalInvariantError,
        ):
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.FAILED,
                terminal_reason=RECOVERY_DRIFT_REASON,
            )
        except LookupError:
            # gateway adapter 缺失是装配故障，不能写坏当前任务。
            raise
        except ModelArtifactStateError as exc:
            raise LifecycleError(
                "task status does not allow the model artifact write"
            ) from exc
        except ModelArtifactGrantError as exc:
            raise LeaseLostError("model artifact grant is no longer current") from exc
        except ClarificationRecordGrantError as exc:
            raise LeaseLostError("clarification grant is no longer current") from exc
        except ClarificationRecordConflictError:
            terminal = _task_outcome(
                task_id=grant.task_id,
                status=TaskStatus.FAILED,
                terminal_reason=RECOVERY_DRIFT_REASON,
            )
        except ClarificationRecordStateError as exc:
            raise LifecycleError(
                "task status does not allow the clarification record write"
            ) from exc
        except _ClarificationTerminalizedError:
            pass
        except (
            RequestRejectedError,
            PolicyDeniedError,
            SqlGuardError,
            BindingError,
            CapabilityBindingError,
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
        winner, evidences, _, _ = await self._finalize(
            record=record,
            outcome=terminal,
            context=context,
            grant=grant,
            binding=(
                binding
                if prepared is not None or recovered_plan is not None
                else None
            ),
            plan=prepared.plan if prepared is not None else recovered_plan,
            allow_advisory=True,
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
        submission = TaskSubmission(envelope=envelope, context=context, as_of=as_of)
        view = await self.submit_task(submission=submission)
        if view.status in TERMINAL_STATUSES and view.render is not None:
            return view.render
        record = await self._tasks.get(
            lookup=TaskLookup(
                task_id=view.task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        if record.status in TERMINAL_STATUSES:
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
                ttl_seconds=self._lease_ttl_seconds,
                trace_id=context.trace_id,
            )
        )
        if attempt.grant is None:
            if attempt.winner.status in TERMINAL_STATUSES:
                return await self._render_recorded(
                    record=attempt.winner, context=context
                )
            raise TaskInProgressError(task_id=record.task_id)
        grant = attempt.grant
        return await run_with_task_heartbeat(
            lambda: self._execute_granted_attempt(
                record=record,
                grant=grant,
                context=context,
                submission=submission,
            ),
            grant=grant,
            task_store=self._tasks,
            lease_ttl_seconds=self._lease_ttl_seconds,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            sleep=self._heartbeat_sleep,
        )

    async def _execute_granted_attempt(
        self,
        *,
        record: TaskRecord,
        grant: TaskAttemptGrant,
        context: RequestContext,
        submission: TaskSubmission,
    ) -> RenderPayload:
        """兼容同步入口取得 grant 后的完整 attempt。"""
        try:
            await self.execute_task(grant=grant, submission=submission)
        except WorkflowPaused as paused:
            evidences = await self._ledger.load(task_id=record.task_id)
            await self._emit(
                stage=PipelineStage.RENDERING,
                outcome=StageOutcome.OK,
                context=context,
                task_id=record.task_id,
                attempt_number=grant.attempt_number,
                delivery=Delivery.LOG_AND_DURABLE,
            )
            return render_pending(
                approval_ref=paused.approval_ref, evidences=evidences
            )
        winner = await self._tasks.get(
            lookup=TaskLookup(
                task_id=record.task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        return await self._render_recorded(record=winner, context=context)

    async def _render_recorded(
        self, *, record: TaskRecord, context: RequestContext
    ) -> RenderPayload:
        if record.status is TaskStatus.CLARIFICATION_REQUIRED:
            clarification = await self._task_views.project_clarification(record=record)
            payload = render_clarification_as_payload(clarification=clarification)
            await self._emit(
                stage=PipelineStage.RENDERING,
                outcome=StageOutcome.OK,
                context=context,
                task_id=record.task_id,
                delivery=Delivery.LOG_AND_DURABLE,
            )
            return payload
        payload = await self._task_views.project_recorded(record=record)
        await self._emit(
            stage=PipelineStage.RENDERING,
            outcome=StageOutcome.OK,
            context=context,
            task_id=record.task_id,
            delivery=Delivery.LOG_AND_DURABLE,
        )
        return payload

    # --- 各阶段 -------------------------------------------------------------

    async def _save_route_clarification(
        self,
        *,
        grant: TaskAttemptGrant,
        route: InteractionRouteDecision,
    ) -> None:
        """把 Router 的 clarify 裁决保存成持久澄清事实。"""
        subject = route.subject
        reason_code = route.reason_code
        if subject is None or not isinstance(reason_code, ClarificationReasonCode):
            raise RequestRejectedError(
                "invalid clarification route decision",
                stage=PipelineStage.INTENT,
            )
        await self._clarification_records.save(
            grant=grant,
            candidate=ClarificationRecordCandidate(
                subject=subject,
                reason_code=reason_code,
                missing_fields=(),
                confirmed_slots=(),
            ),
        )

    async def _complete_conversation(
        self,
        *,
        record: TaskRecord,
        grant: TaskAttemptGrant,
        context: RequestContext,
    ) -> TaskOutcome:
        """完成 I2 普通对话；无计划、无证据、无 Gateway。"""
        current = record
        for status in _conversation_transition_path(current.status):
            current = await self._advance_without_plan(
                record=current,
                status=status,
                grant=grant,
                context=context,
                terminal_reason=(
                    CONVERSATION_TERMINAL_REASON
                    if status is TaskStatus.SUCCEEDED
                    else None
                ),
            )
        return TaskOutcome(
            task_id=current.task_id,
            status=current.status,
            terminal_reason=current.terminal_reason,
            evidence_refs=(),
            render_ref=None,
        )

    async def _advance_without_plan(
        self,
        *,
        record: TaskRecord,
        status: TaskStatus,
        grant: TaskAttemptGrant,
        context: RequestContext,
        terminal_reason: str | None,
    ) -> TaskRecord:
        event = self._event(
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=grant.task_id,
            attempt_number=grant.attempt_number,
        )
        try:
            result = await self._tasks.transition(
                command=TransitionCommand(
                    task_id=grant.task_id,
                    expected_version=record.version,
                    to_status=status,
                    fencing_token=grant.fencing_token,
                    terminal_reason=terminal_reason,
                    audit_events=(event,),
                )
            )
        except Exception as exc:
            await self._sink.emit(
                event, delivery=delivery_for_write_exception(exc)
            )
            raise
        await self._sink.emit(
            event,
            delivery=Delivery.COMMAND_COMMITTED
            if result.applied
            else Delivery.COMMAND_ROLLED_BACK,
        )
        if not result.applied:
            raise LifecycleError("transition rejected", rejection=result.rejection)
        return result.winner

    async def _resolve(
        self,
        *,
        draft: IntentDraft,
        context: RequestContext,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> tuple[Candidate, CapabilityRuntimeBinding]:
        candidates = self._resolver.resolve(
            draft=draft, context=context, snapshot=self._snapshot
        )
        try:
            selected = self._bindings.select_entry(candidates)
        except CapabilityBindingError as exc:
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
            ) from exc
        await self._emit(
            stage=PipelineStage.RESOLVER,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            attempt_number=attempt_number,
            delivery=_independent_delivery(task_id),
        )
        return selected

    async def _prepare(
        self,
        *,
        binding: CapabilityRuntimeBinding,
        candidate: Candidate,
        draft: IntentDraft,
        context: RequestContext,
        as_of: _dt.datetime,
        grant: TaskAttemptGrant,
        clarification: ClarificationContext | None,
        user_text: str,
        task_id: str | None = None,
        attempt_number: int | None = None,
    ) -> PreparedCapability | None:
        try:
            capability_clarification = self._capability_clarification_context(
                clarification=clarification,
                binding=binding,
                candidate=candidate,
            )
            verified = binding.input_binding.slot_verifier(
                candidate=candidate,
                draft=draft,
                context=context,
                as_of=as_of,
                user_text=user_text,
                clarification=capability_clarification,
            )
            if isinstance(verified, SlotIncomplete):
                await self._save_capability_clarification(
                    grant=grant,
                    binding=binding,
                    candidate=candidate,
                    result=verified,
                )
                await self._emit(
                    stage=PipelineStage.PLANNER,
                    outcome=StageOutcome.REJECTED,
                    context=context,
                    task_id=task_id,
                    attempt_number=attempt_number,
                    delivery=_independent_delivery(task_id),
                )
                return None
            if isinstance(verified, SlotInvalid):
                raise CapabilityPreparationError("slot verification rejected")
            if not isinstance(verified, SlotReady):
                raise CapabilityPreparationError("slot verifier returned unknown result")
            params = require_exact_params_type(binding.input_binding, verified)
            prepared = binding.input_binding.planner(
                candidate=candidate,
                params=params,
                context=context,
                snapshot=self._snapshot,
            )
            projector = binding.input_binding.confirmed_slot_projector
            if projector is not None:
                projected = projector(plan=prepared.plan, target=prepared.target)
                require_confirmed_projection(
                    confirmed=verified.confirmed_slots,
                    projected=projected,
                )
        except (
            CapabilityPreparationError,
            CapabilityInputBindingError,
            SlotVerificationError,
        ) as exc:
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
        await self._emit(
            stage=PipelineStage.PLANNER,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            attempt_number=attempt_number,
            delivery=_independent_delivery(task_id),
        )
        return prepared

    async def _load_clarification_context(
        self,
        *,
        submission: TaskSubmission,
    ) -> ClarificationContext | None:
        parent_id = submission.clarification_parent_task_id
        if parent_id is None:
            return None
        parent = await self._clarification_records.load(task_id=parent_id)
        if parent is None:
            raise RequestRejectedError(
                "clarification parent is missing",
                stage=PipelineStage.INTENT,
            )
        return ClarificationContext(
            subject=parent.subject,
            confirmed_slots=parent.confirmed_slots,
            missing_fields=parent.missing_fields,
        )

    def _capability_clarification_context(
        self,
        *,
        clarification: ClarificationContext | None,
        binding: CapabilityRuntimeBinding,
        candidate: Candidate,
    ) -> ClarificationContext | None:
        if clarification is None:
            return None
        if not isinstance(clarification.subject, CapabilitySubject):
            return None
        subject = clarification.subject
        if (
            subject.capability_id != binding.capability_id
            or subject.capability_version != binding.capability_version
            or subject.operation != candidate.operation
            or subject.input_schema_ref != binding.input_binding.input_schema_ref
        ):
            raise CapabilityPreparationError("clarification parent is incompatible")
        return ClarificationContext(
            subject=subject,
            confirmed_slots=clarification.confirmed_slots,
            missing_fields=clarification.missing_fields,
        )

    async def _save_capability_clarification(
        self,
        *,
        grant: TaskAttemptGrant,
        binding: CapabilityRuntimeBinding,
        candidate: Candidate,
        result: SlotIncomplete,
    ) -> None:
        subject = CapabilitySubject(
            kind="capability",
            capability_id=binding.capability_id,
            capability_version=binding.capability_version,
            operation=candidate.operation,
            input_schema_ref=binding.input_binding.input_schema_ref,
            confirmed_slots=result.confirmed_slots,
        )
        await self._clarification_records.save(
            grant=grant,
            candidate=ClarificationRecordCandidate(
                subject=subject,
                reason_code=result.reason_code,
                missing_fields=result.missing_fields,
                confirmed_slots=result.confirmed_slots,
            ),
        )

    async def _finish(
        self,
        *,
        record: TaskRecord,
        outcome: TaskOutcome,
        context: RequestContext,
        grant: TaskAttemptGrant,
        binding: CapabilityRuntimeBinding,
    ) -> RenderPayload:
        """读回证据、判定终态、写 TaskStore、投影。"""
        winner, evidences, verdict, advisory = await self._finalize(
            record=record,
            outcome=outcome,
            context=context,
            grant=grant,
            binding=binding,
            plan=None,
            allow_advisory=False,
        )
        payload = project_terminal(
            record=winner,
            evidences=evidences,
            verdict=verdict,
            binding=binding,
            advisory=advisory,
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
        binding: CapabilityRuntimeBinding | None,
        plan: ExecutionPlan | None,
        allow_advisory: bool,
    ) -> tuple[
        TaskRecord,
        tuple[EvidenceEnvelope, ...],
        AnswerabilityVerdict,
        ModelAdvisory | None,
    ]:
        """在原 grant 下完成终态 CAS，并返回投影所需的持久化事实。"""
        task_id = record.task_id
        if task_id != grant.task_id or outcome.task_id != grant.task_id:
            raise LifecycleError("execution result belongs to a different task")
        # 按引用读回：Runner 的返回值只带 refs，内容一律来自 ledger。
        evidences: tuple[EvidenceEnvelope, ...] = await self._ledger.load(
            task_id=task_id
        )
        verdict = assess_evidence(binding=binding, evidences=evidences)
        advisory: ModelAdvisory | None = None
        model_event: TraceEvent | None = None
        if allow_advisory and binding is not None and plan is not None:
            try:
                accepted = await load_or_accept_advisory(
                    grant=grant,
                    task_id=task_id,
                    plan=plan,
                    outcome=outcome,
                    evidences=evidences,
                    verdict=verdict,
                    projector=binding.advisory_projector,
                    model=self._slow_query_advisory,
                    profile=self._model_profile,
                    artifacts=self._model_artifacts,
                    monotonic=self._model_monotonic,
                )
            except ModelArtifactConflictError:
                outcome = _task_outcome(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    terminal_reason=RECOVERY_DRIFT_REASON,
                )
            except ModelArtifactStateError as exc:
                raise LifecycleError(
                    "task status does not allow the model artifact write"
                ) from exc
            except ModelArtifactGrantError as exc:
                raise LeaseLostError(
                    "model artifact grant is no longer current"
                ) from exc
            else:
                if accepted.artifact is not None:
                    advisory = accepted.artifact.advisory
                if (
                    accepted.observation is not None
                    and accepted.observation.fallback_code
                    is not ModelFallbackCode.DISABLED
                ):
                    fallback = accepted.observation.fallback_code
                    model_event = self._event(
                        stage=PipelineStage.MODEL,
                        outcome=(
                            StageOutcome.OK
                            if fallback is None
                            else StageOutcome.FAILED
                        ),
                        context=context,
                        task_id=task_id,
                        attempt_number=grant.attempt_number,
                        model=accepted.observation,
                    )
        # Advisory 恢复冲突可以把 outcome 改为 FAILED；Reflection 必须基于最终结果。
        # 它只在执行本身没出问题、却仍证据不足时记为 REJECTED。上游失败时，证据
        # 不足是该失败的后果，不应制造第二个根因。
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
                    audit_events=tuple(
                        event
                        for event in (reflection_event, model_event, lifecycle_event)
                        if event is not None
                    ),
                )
            )
        except Exception as exc:
            delivery = delivery_for_write_exception(exc)
            for event in (reflection_event, model_event, lifecycle_event):
                if event is None:
                    continue
                await self._sink.emit(event, delivery=delivery)
            raise
        delivery = (
            Delivery.COMMAND_COMMITTED
            if result.applied
            else Delivery.COMMAND_ROLLED_BACK
        )
        for event in (reflection_event, model_event, lifecycle_event):
            if event is None:
                continue
            await self._sink.emit(event, delivery=delivery)
        if not result.applied:
            raise LifecycleError(
                "terminal transition rejected", rejection=result.rejection
            )
        return result.winner, evidences, verdict, advisory


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


def _conversation_transition_path(status: TaskStatus) -> tuple[TaskStatus, ...]:
    if status is TaskStatus.CREATED:
        return (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
    if status is TaskStatus.PLANNING:
        return (TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
    if status is TaskStatus.RUNNING:
        return (TaskStatus.SUCCEEDED,)
    raise LifecycleError("task status cannot complete a conversation")


def _plan_contains_restricted_read(plan: ExecutionPlan) -> bool:
    return any(step.read_class is ReadClass.RESTRICTED for step in plan.steps)


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

"""确定性步骤 Runner：任务生命周期的宿主。

Runner 拥有租约、CAS 推进、可选分支求值、预算与暂停；它**不拥有领域安全规则**：
分类、策略、SQL 与审批一律经 ``admit_step``，工具一律经 ``ToolGateway``，证据一律
经 ``EvidenceLedger``。

三条承重设计：

1. **条件求值从持久事实重建，不读上次进程的内存。** Evidence 条件从 ledger
   读回；步骤结果条件从 step journal 重建，并随本轮 committed/adopted result 更新。
   M4 的跨进程恢复因此不需要猜测上次进程的局部状态。
2. **每次状态变更都携带 ``expected_version`` 与 ``fencing_token``，并采纳存储层
   ``winner``。** CAS 失败是正常并发结果，不是异常路径。
3. **Runner 不写终态。** 终态由 Runtime 依 Answerability 的结构化结论确定性判定
   （ARCHITECTURE §4.3）。Runner 若先写 ``SUCCEEDED``，终态保护会让 Runtime 再也
   无法把一次"空证据"降级为 ``indeterminate``——"空证据不得渲染成成功"就在存储层
   被堵死了。Runner 返回的 ``TaskOutcome`` 是**执行层结论**，最终那一次 CAS 由
   Runtime 完成。
"""

import datetime as _dt
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Protocol

from xiaowei_agent.capabilities.effect import derive_effect
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    AgentError,
    ApprovalRequest,
    ApprovalState,
    CapabilitySnapshot,
    ConfirmedSlot,
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalInput,
    ExternalInputKind,
    PipelineStage,
    PlanStep,
    PolicySnapshot,
    RequestContext,
    ResolvedTarget,
    StageOutcome,
    StepAttemptDecision,
    StepCommitRejection,
    StepCondition,
    StepConditionKind,
    StepOutcomeKind,
    StepResultStatus,
    TaskLookup,
    TaskOutcome,
    TaskRecord,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    ToolResult,
    TraceEvent,
)
from xiaowei_agent.evidence.errors import EvidenceBuildError
from xiaowei_agent.governance.approval import (
    ApprovalGate,
    ApprovalRequiredError,
    approval_ref,
)
from xiaowei_agent.governance.step_admission import admit_step
from xiaowei_agent.observability.sink import (
    Delivery,
    TraceSink,
    delivery_for_write_exception,
)
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.plans import PlanStore
from xiaowei_agent.persistence.store import (
    Clock,
    StepAttemptCommand,
    StepCommitCommand,
    StepExecutionRecord,
    TaskAttemptGrant,
    TaskStore,
    TransitionCommand,
)
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from xiaowei_agent.planning.disclosure import project_execution_disclosure
from xiaowei_agent.runners.binding import ExecutionBindingProvider
from xiaowei_agent.runners.runner import WorkflowPaused
from xiaowei_agent.tools.gateway import MalformedAdapterResponseError

BUDGET_EXHAUSTED_REASON: Final[str] = "budget.tool_calls_exhausted"
APPROVAL_TTL_SECONDS: Final[int] = 3600
DEFAULT_LEASE_TTL_SECONDS: Final[int] = 60
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
RECOVERY_DRIFT_REASON: Final[str] = "recovery_drift"

_STEP_RESULT_STATUS: Final[Mapping[ToolCallStatus, StepResultStatus]] = MappingProxyType(
    {
        ToolCallStatus.OK: StepResultStatus.OK,
        ToolCallStatus.ERROR: StepResultStatus.FAILED,
        ToolCallStatus.TIMEOUT: StepResultStatus.TIMEOUT,
        ToolCallStatus.INDETERMINATE: StepResultStatus.FAILED,
    }
)


class _Gateway(Protocol):
    """Runner 只需要 Gateway 的这一个方法。

    用结构化类型而不是 ``object``：``object`` 会让"Runner 还调了 Gateway 的别的
    什么"在类型层不可见，而 Runner 与 Gateway 的边界正是这条链路最需要窄的地方。
    """

    async def invoke(
        self,
        call: ToolCall,
        *,
        context: RequestContext,
        admission: AdmissionCertificate,
    ) -> ToolResult: ...


class LifecycleError(RuntimeError):
    """无法推进生命周期：租约拿不到、CAS 被拒或任务已终态。

    消息恒为常量；``rejection`` 放结构化属性。
    """

    def __init__(self, message: str, *, rejection: object = None) -> None:
        super().__init__(message)
        self.rejection = rejection


class LeaseLostError(LifecycleError):
    """当前 Worker 的 grant 已无法续租；调用方只能停止，不得补写。"""


class DriftError(RuntimeError):
    """恢复时重解析/重算的结果与存储中的事实不一致。"""


class StepJournalInvariantError(RuntimeError):
    """步骤提交协议被破坏；调用方不得把它降级成一次可重试任务失败。"""


class DeterministicStepRunner:
    """按计划顺序推进的 Runner。"""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        plan_store: PlanStore,
        ledger: EvidenceLedger,
        gateway: _Gateway,
        approval_gate: ApprovalGate,
        snapshot: CapabilitySnapshot,
        policy_snapshot: PolicySnapshot,
        bindings: ExecutionBindingProvider,
        clock: Clock,
        sink: TraceSink,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._tasks = task_store
        self._plans = plan_store
        self._ledger = ledger
        self._gateway = gateway
        self._approval_gate = approval_gate
        self._snapshot = snapshot
        self._policy_snapshot = policy_snapshot
        self._bindings = bindings
        self._clock = clock
        self._sink = sink
        self._lease_ttl_seconds = lease_ttl_seconds

    # --- trace --------------------------------------------------------------

    def _event(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str,
        plan: ExecutionPlan | None = None,
        step_id: str | None = None,
        attempt_number: int | None = None,
        error: AgentError | None = None,
    ) -> TraceEvent:
        """构造一条阶段事件；命令决定何时持久化，sink 决定何时记录日志。

        **detail 恒为空**：阶段、结果、步骤与 capability 已经足以把一次失败定位到
        唯一一个阶段，而 detail 是最容易把 SQL 或外部文本带出去的地方。需要更多
        诊断信息时应先扩契约字段，而不是往 detail 里塞自由文本。
        """
        return TraceEvent(
            event_id=str(uuid.uuid4()),
            trace_id=context.trace_id,
            task_id=task_id,
            stage=stage,
            outcome=outcome,
            occurred_at=self._clock(),
            capability_id=None if plan is None else plan.capability_id,
            step_id=step_id,
            policy_revision=context.policy_revision,
            attempt_number=attempt_number,
            error=error,
            detail={},
        )

    async def _emit(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str,
        plan: ExecutionPlan | None = None,
        step_id: str | None = None,
        attempt_number: int | None = None,
        delivery: Delivery,
    ) -> TraceEvent:
        event = self._event(
            stage=stage,
            outcome=outcome,
            context=context,
            task_id=task_id,
            plan=plan,
            step_id=step_id,
            attempt_number=attempt_number,
        )
        await self._sink.emit(event, delivery=delivery)
        return event

    async def _emit_all(
        self, events: tuple[TraceEvent, ...], *, delivery: Delivery
    ) -> None:
        for event in events:
            await self._sink.emit(event, delivery=delivery)

    # --- 公开入口 -----------------------------------------------------------

    async def start(
        self,
        grant: TaskAttemptGrant,
        *,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
        parent_confirmed_slots: tuple[ConfirmedSlot, ...] | None = None,
    ) -> TaskOutcome:
        """执行一份新编译的计划。

        计划与目标先落 ``PlanStore``——resume 要重算指纹，就必须先拿得回**当初
        那份**计划。

        :raises LifecycleError: 租约拿不到，或状态迁移被存储层拒绝。
        :raises WorkflowPaused: 遇到需要审批的副作用步骤。
        """
        await self._require_current_grant(grant)
        return await self._start(
            grant=grant,
            plan=plan,
            target=target,
            context=context,
            parent_confirmed_slots=parent_confirmed_slots,
        )

    async def _start(
        self,
        *,
        grant: TaskAttemptGrant,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
        parent_confirmed_slots: tuple[ConfirmedSlot, ...] | None,
    ) -> TaskOutcome:
        task_id = grant.task_id
        if await self._tasks.load_step_executions(task_id=task_id):
            raise StepJournalInvariantError("new execution already has a step journal")
        await self._plans.save(task_id=task_id, plan=plan, target=target)
        stored = await self._plans.load(task_id=task_id)
        plan = stored.plan
        target = stored.target
        record = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        for status in (TaskStatus.PLANNING, TaskStatus.RUNNING):
            record = await self._advance(
                record=record,
                status=status,
                grant=grant,
                context=context,
                plan=plan,
            )
        return await self._run_steps(
            grant=grant,
            plan=plan,
            target=target,
            context=context,
            parent_confirmed_slots=parent_confirmed_slots,
        )

    async def resume(
        self,
        grant: TaskAttemptGrant,
        external_input: ExternalInput | None = None,
        *,
        plan: ExecutionPlan,
        context: RequestContext,
        target: ResolvedTarget,
        approval: ApprovalRequest | None = None,
        parent_confirmed_slots: tuple[ConfirmedSlot, ...] | None = None,
    ) -> TaskOutcome:
        """恢复一个暂停的任务。

        **重解析并重算**：从 ``PlanStore`` 取回计划与目标，重新算 ``plan_hash`` 与
        ``target_fingerprint``，与调用方重新解析出的目标、当前 policy revision 逐一
        比对；任一不匹配即拒绝，且 Gateway 调用次数为 0。

        不信任存储里"自称"的指纹——存一份自称的 hash 只会制造可被篡改的第二真源。

        :raises DriftError: 计划、目标、policy revision 任一漂移，或恢复输入与
            实际用于准入的审批不一致。
        :raises WorkflowPaused: 仍然缺少有效审批。
        """
        await self._require_current_grant(grant)
        return await self._resume(
            grant=grant,
            external_input=external_input,
            plan=plan,
            context=context,
            target=target,
            approval=approval,
            parent_confirmed_slots=parent_confirmed_slots,
        )

    async def _resume(
        self,
        *,
        grant: TaskAttemptGrant,
        external_input: ExternalInput | None,
        plan: ExecutionPlan,
        context: RequestContext,
        target: ResolvedTarget,
        approval: ApprovalRequest | None,
        parent_confirmed_slots: tuple[ConfirmedSlot, ...] | None,
    ) -> TaskOutcome:
        task_id = grant.task_id
        self._verify_external_input(external_input, approval=approval)
        stored = await self._plans.load(task_id=task_id)
        self._verify_no_drift(
            stored_plan=stored.plan,
            plan=plan,
            stored_target=stored.target,
            target=target,
            context=context,
        )
        plan = stored.plan
        record = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        if record.status in {TaskStatus.PLANNING, TaskStatus.AWAITING_APPROVAL}:
            record = await self._advance(
                record=record,
                status=TaskStatus.RUNNING,
                grant=grant,
                context=context,
                plan=plan,
            )
        elif record.status is not TaskStatus.RUNNING:
            raise LifecycleError("task status cannot be resumed")
        return await self._run_steps(
            grant=grant,
            plan=plan,
            target=stored.target,
            context=context,
            approval=approval,
            parent_confirmed_slots=parent_confirmed_slots,
        )

    # --- 漂移检测 -----------------------------------------------------------

    def _verify_external_input(
        self, external_input: ExternalInput | None, *, approval: ApprovalRequest | None
    ) -> None:
        """恢复输入必须与真正参与准入的审批指向同一个 ref。

        ``ExternalInput`` 是 M2 为 resume 定义的跨边界输入，``approval`` 是实际交给
        ``ApprovalGate`` 的审批事实。两者是**同一件事的两个表述**：前者是调用方声称
        "这张审批已批"，后者是那张审批本身。收下 ref 却不核对，等于让调用方可以拿
        A 步骤的 ref 恢复 B 步骤——ref 不再是任何东西的凭据，只是一段被忽略的文本。

        ``USER_SUPPLEMENT`` 直接放行且**不读取 ``user_text``**：它是不可信外部文本，
        按 ARCHITECTURE 的边界不得影响 policy、目标、权限、审批状态或执行计划。
        "不消费"在这里是正确行为，因此显式写出来，而不是让它和上面那条一起沉默。
        """
        if external_input is None:
            return
        if external_input.kind is not ExternalInputKind.APPROVAL_DECISION:
            return
        if approval is None:
            raise DriftError("resume carries an approval decision but no approval")
        expected = approval_ref(
            task_id=approval.task_id, step_id=approval.step_id
        )
        if external_input.approval_ref != expected:
            raise DriftError("approval_ref does not match the supplied approval")

    def _verify_no_drift(
        self,
        *,
        stored_plan: ExecutionPlan,
        plan: ExecutionPlan,
        stored_target: ResolvedTarget,
        target: ResolvedTarget,
        context: RequestContext,
    ) -> None:
        if compute_plan_hash(stored_plan) != compute_plan_hash(plan):
            raise DriftError("plan_hash drifted since the plan was stored")
        if compute_target_fingerprint(stored_target) != compute_target_fingerprint(target):
            raise DriftError("target_fingerprint drifted since the plan was stored")
        if plan.policy_revision != context.policy_revision:
            raise DriftError("policy revision drifted since the plan was stored")

    # --- 生命周期 -----------------------------------------------------------

    async def _advance(
        self,
        *,
        record: TaskRecord,
        status: TaskStatus,
        grant: TaskAttemptGrant,
        context: RequestContext,
        plan: ExecutionPlan,
    ) -> TaskRecord:
        event = self._event(
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=grant.task_id,
            plan=plan,
            attempt_number=grant.attempt_number,
        )
        try:
            result = await self._tasks.transition(
                command=TransitionCommand(
                    task_id=grant.task_id,
                    expected_version=record.version,
                    to_status=status,
                    fencing_token=grant.fencing_token,
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
            # 必须采纳 winner 并停下，不能用本地旧对象继续推进。
            raise LifecycleError("transition rejected", rejection=result.rejection)
        return result.winner

    async def _require_current_grant(self, grant: TaskAttemptGrant) -> None:
        renewed = await self._tasks.renew_lease(
            task_id=grant.task_id,
            owner=grant.lease.owner,
            fencing_token=grant.fencing_token,
            ttl_seconds=self._lease_ttl_seconds,
        )
        if renewed is None:
            raise LeaseLostError("task attempt grant is no longer current")

    # --- 步骤循环 -----------------------------------------------------------

    async def _run_steps(
        self,
        *,
        grant: TaskAttemptGrant,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
        approval: ApprovalRequest | None = None,
        parent_confirmed_slots: tuple[ConfirmedSlot, ...] | None = None,
    ) -> TaskOutcome:
        task_id = grant.task_id
        await self._disclose(
            grant=grant,
            plan=plan,
            target=target,
            context=context,
            parent_confirmed_slots=parent_confirmed_slots,
        )
        recorded = await self._tasks.load_step_executions(task_id=task_id)
        result_by_step = {
            item.step_id: item.result_status
            for item in recorded
            if item.result_status is not None
        }
        failed_steps = {
            item.step_id
            for item in recorded
            if item.result_status in {StepResultStatus.FAILED, StepResultStatus.TIMEOUT}
        }
        degraded = bool(failed_steps)
        for step in plan.steps:
            if not await self._condition_holds(
                task_id=task_id,
                condition=step.condition,
                result_by_step=result_by_step,
            ):
                continue
            try:
                certificate, call = self._admit(
                    task_id=task_id,
                    step=step,
                    plan=plan,
                    target=target,
                    context=context,
                    approval=approval,
                )
            except ApprovalRequiredError as exc:
                await self._emit(
                    stage=PipelineStage.ADMISSION,
                    outcome=StageOutcome.REJECTED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                    attempt_number=grant.attempt_number,
                    delivery=Delivery.LOG_AND_DURABLE,
                )
                await self._pause(
                    task_id=task_id,
                    step=step,
                    plan=plan,
                    target=target,
                    grant=grant,
                    context=context,
                    step_id=exc.step_id,
                )
            except Exception:
                # 准入的任何拒绝都归因到 ADMISSION，然后照常向上传播——Runner
                # 不吞掉治理层的拒绝，只负责让它在 trace 上落到正确的阶段。
                await self._emit(
                    stage=PipelineStage.ADMISSION,
                    outcome=StageOutcome.REJECTED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                    attempt_number=grant.attempt_number,
                    delivery=Delivery.LOG_AND_DURABLE,
                )
                raise
            await self._emit(
                stage=PipelineStage.ADMISSION,
                outcome=StageOutcome.OK,
                context=context,
                task_id=task_id,
                plan=plan,
                step_id=step.step_id,
                attempt_number=grant.attempt_number,
                delivery=Delivery.LOG_AND_DURABLE,
            )

            attempt = await self._tasks.begin_step_attempt(
                command=StepAttemptCommand(grant=grant, step_id=step.step_id)
            )
            if attempt.decision is StepAttemptDecision.ALREADY_COMMITTED:
                if attempt.record is None:  # model validator also guards this
                    raise LifecycleError("committed step has no journal row")
                degraded = self._adopt_step_record(
                    attempt.record,
                    failed_steps=failed_steps,
                    result_by_step=result_by_step,
                    degraded=degraded,
                )
                continue
            if attempt.decision is StepAttemptDecision.BUDGET_EXHAUSTED:
                return await self._outcome(
                    task_id,
                    TaskStatus.FAILED,
                    terminal_reason=BUDGET_EXHAUSTED_REASON,
                )
            if attempt.decision is StepAttemptDecision.UNKNOWN_STEP:
                return await self._outcome(
                    task_id,
                    TaskStatus.FAILED,
                    terminal_reason=RECOVERY_DRIFT_REASON,
                )
            if attempt.decision is not StepAttemptDecision.PROCEED:
                raise LifecycleError(
                    "step attempt rejected", rejection=attempt.decision
                )

            try:
                result = await self._gateway.invoke(
                    call, context=context, admission=certificate
                )
            except MalformedAdapterResponseError:
                gateway_event = self._event(
                    stage=PipelineStage.GATEWAY,
                    outcome=StageOutcome.FAILED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                    attempt_number=grant.attempt_number,
                )
                gateway_events = (gateway_event,)
                record = await self._commit_step(
                    StepCommitCommand(
                        grant=grant,
                        step_id=step.step_id,
                        kind=StepOutcomeKind.MALFORMED_ADAPTER,
                        status=StepResultStatus.FAILED,
                        evidence=None,
                        audit_events=(gateway_event,),
                    ),
                    events=gateway_events,
                )
                degraded = self._adopt_step_record(
                    record,
                    failed_steps=failed_steps,
                    result_by_step=result_by_step,
                    degraded=degraded,
                )
                continue
            ok = result.status is ToolCallStatus.OK
            gateway_event = self._event(
                stage=PipelineStage.GATEWAY,
                outcome=StageOutcome.OK if ok else StageOutcome.FAILED,
                context=context,
                task_id=task_id,
                plan=plan,
                step_id=step.step_id,
                attempt_number=grant.attempt_number,
                error=result.error,
            )
            try:
                evidence = self._build_evidence(
                    task_id=task_id,
                    step=step,
                    plan=plan,
                    target=target,
                    result=result,
                )
            except EvidenceBuildError:
                evidence_event = self._event(
                    stage=PipelineStage.EVIDENCE,
                    outcome=StageOutcome.FAILED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                    attempt_number=grant.attempt_number,
                )
                record = await self._commit_step(
                    StepCommitCommand(
                        grant=grant,
                        step_id=step.step_id,
                        kind=StepOutcomeKind.MALFORMED_ADAPTER,
                        status=StepResultStatus.FAILED,
                        evidence=None,
                        audit_events=(gateway_event, evidence_event),
                    ),
                    events=(gateway_event, evidence_event),
                )
                degraded = self._adopt_step_record(
                    record,
                    failed_steps=failed_steps,
                    result_by_step=result_by_step,
                    degraded=degraded,
                )
                continue
            evidence_event = self._event(
                stage=PipelineStage.EVIDENCE,
                outcome=StageOutcome.OK,
                context=context,
                task_id=task_id,
                plan=plan,
                step_id=step.step_id,
                attempt_number=grant.attempt_number,
            )
            step_status = _STEP_RESULT_STATUS[result.status]
            committed_events = (gateway_event, evidence_event)
            record = await self._commit_step(
                StepCommitCommand(
                    grant=grant,
                    step_id=step.step_id,
                    kind=StepOutcomeKind.TOOL_RESULT,
                    status=step_status,
                    evidence=evidence,
                    audit_events=(gateway_event, evidence_event),
                ),
                events=committed_events,
            )
            degraded = self._adopt_step_record(
                record,
                failed_steps=failed_steps,
                result_by_step=result_by_step,
                degraded=degraded,
            )
        status = TaskStatus.INDETERMINATE if degraded else TaskStatus.SUCCEEDED
        return await self._outcome(task_id, status, terminal_reason=None)

    async def _disclose(
        self,
        *,
        grant: TaskAttemptGrant,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
        parent_confirmed_slots: tuple[ConfirmedSlot, ...] | None,
    ) -> None:
        try:
            execution = self._bindings.execution_for(plan=plan)
            project_execution_disclosure(
                plan=plan,
                target=target,
                binding=execution.disclosure,
                parent_confirmed_slots=parent_confirmed_slots or (),
            )
        except Exception:
            await self._emit(
                stage=PipelineStage.DISCLOSURE,
                outcome=StageOutcome.FAILED,
                context=context,
                task_id=grant.task_id,
                plan=plan,
                attempt_number=grant.attempt_number,
                delivery=Delivery.LOG_AND_DURABLE,
            )
            raise
        await self._emit(
            stage=PipelineStage.DISCLOSURE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=grant.task_id,
            plan=plan,
            attempt_number=grant.attempt_number,
            delivery=Delivery.LOG_AND_DURABLE,
        )

    async def _commit_step(
        self, command: StepCommitCommand, *, events: tuple[TraceEvent, ...]
    ) -> StepExecutionRecord:
        try:
            result = await self._tasks.commit_step_result(command=command)
        except Exception as exc:
            await self._emit_all(
                events, delivery=delivery_for_write_exception(exc)
            )
            raise
        await self._emit_all(
            events,
            delivery=Delivery.COMMAND_COMMITTED
            if result.committed
            else Delivery.COMMAND_ROLLED_BACK,
        )
        return self._require_committed_step(result)

    @staticmethod
    def _adopt_step_record(
        record: StepExecutionRecord,
        *,
        failed_steps: set[str],
        result_by_step: dict[str, StepResultStatus],
        degraded: bool,
    ) -> bool:
        if record.result_status is None:
            raise StepJournalInvariantError("committed step has no result status")
        result_by_step[record.step_id] = record.result_status
        if record.result_status in {StepResultStatus.FAILED, StepResultStatus.TIMEOUT}:
            failed_steps.add(record.step_id)
            return True
        return degraded

    @staticmethod
    def _require_committed_step(result: object) -> StepExecutionRecord:
        from xiaowei_agent.persistence.store import StepCommitResult

        if not isinstance(result, StepCommitResult):
            raise StepJournalInvariantError("step store returned an invalid result")
        if result.committed and result.record is not None:
            return result.record
        if result.rejection in {
            StepCommitRejection.STALE_FENCING,
            StepCommitRejection.NOT_RUNNABLE,
        }:
            raise LifecycleError("step commit lost ownership", rejection=result.rejection)
        raise StepJournalInvariantError("step commit invariant was violated")

    async def _condition_holds(
        self,
        *,
        task_id: str,
        condition: StepCondition,
        result_by_step: Mapping[str, StepResultStatus],
    ) -> bool:
        """按闭集条件决定是否执行该步骤。

        Evidence 条件从 ledger 读回；步骤结果条件只读由 step journal 构造并随
        committed/adopted result 更新的 ``result_by_step``。两者都不依赖上次进程的
        内存状态。

        但**被引用的步骤若执行失败，条件一律不成立**（fail-closed）：
        ``EVIDENCE_ROW_COUNT_BELOW`` 在数值上无法区分"取到零行"与"根本没取到"，
        而这两者的含义相反。s1 失败时仍去跑 s2，会让一次取数故障看起来像是在
        "确认范围内有没有流量"——那正是本闭环要避免的、自信但错误的结论。
        """
        if condition.kind is StepConditionKind.ALWAYS:
            return True
        if condition.kind is StepConditionKind.EVIDENCE_ROW_COUNT_BELOW:
            threshold = condition.threshold
            if threshold is None or condition.ref_step_id is None:
                return False
            if result_by_step.get(condition.ref_step_id) is not StepResultStatus.OK:
                return False
            rows = await self._rows_recorded_for(
                task_id=task_id, step_id=condition.ref_step_id
            )
            return rows < threshold
        if condition.kind is StepConditionKind.PRIOR_STEP_RESULT_IS:
            if condition.ref_step_id is None or condition.expected_result is None:
                return False
            return (
                result_by_step.get(condition.ref_step_id)
                is condition.expected_result
            )
        # EVIDENCE_FIELD_ABSENT 尚未消费；不猜测语义，一律不执行（fail-closed）。
        return False

    async def _rows_recorded_for(self, *, task_id: str, step_id: str) -> int:
        for envelope in await self._ledger.load(task_id=task_id):
            if envelope.evidence_id.endswith(f":{step_id}"):
                return len(envelope.facts)
        return 0

    def _admit(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
        approval: ApprovalRequest | None,
    ) -> tuple[AdmissionCertificate, ToolCall]:
        """六段准入。**同步**：准入不做 I/O，把它与工具调用分开才能各自归因。"""
        call = self._build_call(
            plan=plan,
            step=step,
            idempotency_key=f"{compute_plan_hash(plan)[:16]}:{step.step_id}",
        )
        execution = self._bindings.execution_for(plan=plan)
        certificate = admit_step(
            step=step,
            plan=plan,
            call=call,
            context=context,
            target=target,
            snapshot=self._snapshot,
            policy_snapshot=self._policy_snapshot,
            profile=execution.policy_profile,
            sql_surface=execution.sql_surface,
            promql_surface=execution.promql_surface,
            approval_gate=self._approval_gate,
            approval=approval,
            task_id=task_id,
            now=self._clock(),
        )
        return certificate, call

    def _build_call(
        self, *, plan: ExecutionPlan, step: PlanStep, idempotency_key: str
    ) -> ToolCall:
        """构造本步骤的工具调用。

        **幂等键由 plan_hash + step_id 派生，不含 task_id**：退出标准要求"固定
        IntentDraft 重复运行产生逐字节相同的 ToolCall 与 tool_call_hash"，而
        task_id 由存储层生成、每次都不同。用计划指纹派生既满足这条，也保持了正确
        的幂等语义——同一份计划的同一步骤就是同一次尝试。
        """
        declared = derive_effect(
            self._snapshot,
            capability_id=plan.capability_id,
            capability_version=plan.capability_version,
            operation=step.operation,
        )
        return ToolCall(
            gateway=declared.gateway,
            operation=step.operation,
            step_id=step.step_id,
            typed_args=dict(step.typed_arguments),
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            idempotency_key=idempotency_key,
        )

    def _build_evidence(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        result: ToolResult,
    ) -> EvidenceEnvelope:
        execution = self._bindings.execution_for(plan=plan)
        return execution.evidence_builder(
            task_id=task_id,
            step=step,
            plan=plan,
            target=target,
            result=result,
            captured_at=self._clock(),
        )

    async def _pause(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        grant: TaskAttemptGrant,
        context: RequestContext,
        step_id: str,
    ) -> None:
        """记录审批请求、CAS 到 AWAITING_APPROVAL，然后抛出 ``WorkflowPaused``。

        顺序是刻意的：**先**留下可审计的审批请求，**再**改状态。反过来时，一次
        崩溃会留下一个"等待审批但没有审批请求"的任务。
        """
        now = self._clock()
        request = ApprovalRequest(
            task_id=task_id,
            step_id=step_id,
            plan_hash=compute_plan_hash(plan),
            target_fingerprint=compute_target_fingerprint(target),
            policy_revision=plan.policy_revision,
            subject=grant.lease.owner,
            expires_at=now + _dt.timedelta(seconds=APPROVAL_TTL_SECONDS),
            state=ApprovalState.PENDING,
        )
        await self._tasks.record_approval(request=request)
        current = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        await self._advance(
            record=current,
            status=TaskStatus.AWAITING_APPROVAL,
            grant=grant,
            context=context,
            plan=plan,
        )
        # 在 raise 之外拼装：拒绝路径的 raise 语句里不得出现任何插值，哪怕取值
        # 本身安全——这条不变量靠"语句里没有插值"来机械保证，不靠逐个判断。
        paused_ref = approval_ref(task_id=task_id, step_id=step_id)
        raise WorkflowPaused(
            task_id=task_id, step_id=step_id, approval_ref=paused_ref
        )

    async def _outcome(
        self, task_id: str, status: TaskStatus, *, terminal_reason: str | None
    ) -> TaskOutcome:
        refs = tuple(
            envelope.evidence_id for envelope in await self._ledger.load(task_id=task_id)
        )
        return TaskOutcome(
            task_id=task_id,
            status=status,
            terminal_reason=terminal_reason,
            evidence_refs=refs,
            # M3 不持久化 RenderPayload：它由 Runtime 同步返回，没有任何读回方。
            render_ref=None,
        )

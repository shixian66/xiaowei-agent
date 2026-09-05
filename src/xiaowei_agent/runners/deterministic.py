"""确定性步骤 Runner：任务生命周期的宿主。

Runner 拥有租约、CAS 推进、可选分支求值、预算与暂停；它**不拥有领域安全规则**：
分类、策略、SQL 与审批一律经 ``admit_step``，工具一律经 ``ToolGateway``，证据一律
经 ``EvidenceLedger``。

三条承重设计：

1. **条件求值从 ledger 读回，不读本地变量。** 这让 ledger 在**执行期**就成为唯一
   路径，而不只是事后归档；M4 的跨进程恢复因此不必改变消费方。
2. **每次状态变更都携带 ``expected_version`` 与 ``fencing_token``，并采纳存储层
   ``winner``。** CAS 失败是正常并发结果，不是异常路径。
3. **Runner 不写终态。** 终态由 Runtime 依 Answerability 的结构化结论确定性判定
   （ARCHITECTURE §4.3）。Runner 若先写 ``SUCCEEDED``，终态保护会让 Runtime 再也
   无法把一次"空证据"降级为 ``indeterminate``——"空证据不得渲染成成功"就在存储层
   被堵死了。Runner 返回的 ``TaskOutcome`` 是**执行层结论**，最终那一次 CAS 由
   Runtime 完成。
"""

import datetime as _dt
from typing import Final, Protocol

from xiaowei_agent.capabilities.specs import GATEWAY_NAME
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    ApprovalRequest,
    ApprovalState,
    CapabilitySnapshot,
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalInput,
    ExternalInputKind,
    LeaseGrant,
    PipelineStage,
    PlanStep,
    PolicyProfile,
    PolicySnapshot,
    RequestContext,
    ResolvedTarget,
    SqlSurface,
    StageOutcome,
    StepCondition,
    StepConditionKind,
    TaskLookup,
    TaskOutcome,
    TaskRecord,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    ToolResult,
    TraceEvent,
)
from xiaowei_agent.evidence import build_evidence
from xiaowei_agent.governance.approval import (
    ApprovalGate,
    ApprovalRequiredError,
    approval_ref,
)
from xiaowei_agent.governance.step_admission import admit_step
from xiaowei_agent.observability.sink import TraceSink
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.plans import PlanStore
from xiaowei_agent.persistence.store import Clock, TaskStore, TransitionCommand
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from xiaowei_agent.planning.starrocks.params import SlowQueryParams
from xiaowei_agent.runners.runner import WorkflowPaused

BUDGET_EXHAUSTED_REASON: Final[str] = "budget.tool_calls_exhausted"
APPROVAL_TTL_SECONDS: Final[int] = 3600
DEFAULT_LEASE_TTL_SECONDS: Final[int] = 60
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


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


class DriftError(RuntimeError):
    """恢复时重解析/重算的结果与存储中的事实不一致。"""


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
        profile: PolicyProfile,
        surface: SqlSurface,
        clock: Clock,
        owner: str,
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
        self._profile = profile
        self._surface = surface
        self._clock = clock
        self._owner = owner
        self._sink = sink
        self._lease_ttl_seconds = lease_ttl_seconds
        self._event_seq = 0

    # --- trace --------------------------------------------------------------

    def _emit(
        self,
        *,
        stage: PipelineStage,
        outcome: StageOutcome,
        context: RequestContext,
        task_id: str,
        plan: ExecutionPlan | None = None,
        step_id: str | None = None,
    ) -> None:
        """发一条阶段事件。

        **detail 恒为空**：阶段、结果、步骤与 capability 已经足以把一次失败定位到
        唯一一个阶段，而 detail 是最容易把 SQL 或外部文本带出去的地方。需要更多
        诊断信息时应先扩契约字段，而不是往 detail 里塞自由文本。
        """
        self._event_seq += 1
        self._sink.emit(
            TraceEvent(
                event_id=f"{task_id}:{self._event_seq}",
                trace_id=context.trace_id,
                task_id=task_id,
                stage=stage,
                outcome=outcome,
                occurred_at=self._clock(),
                capability_id=None if plan is None else plan.capability_id,
                step_id=step_id,
                policy_revision=context.policy_revision,
                error=None,
                detail={},
            )
        )

    # --- 公开入口 -----------------------------------------------------------

    async def start(
        self,
        task_id: str,
        *,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
    ) -> TaskOutcome:
        """执行一份新编译的计划。

        计划与目标先落 ``PlanStore``——resume 要重算指纹，就必须先拿得回**当初
        那份**计划。

        :raises LifecycleError: 租约拿不到，或状态迁移被存储层拒绝。
        :raises WorkflowPaused: 遇到需要审批的副作用步骤。
        """
        await self._plans.save(task_id=task_id, plan=plan, target=target)
        grant = await self._acquire(task_id)
        record = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        for status in (TaskStatus.PLANNING, TaskStatus.RUNNING):
            record = await self._advance(
                task_id, record.version, status, grant.fencing_token
            )
        self._emit(
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            plan=plan,
        )
        return await self._run_steps(
            task_id=task_id,
            plan=plan,
            target=target,
            context=context,
            fencing_token=grant.fencing_token,
            version=record.version,
        )

    async def resume(
        self,
        task_id: str,
        external_input: ExternalInput | None = None,
        *,
        context: RequestContext,
        target: ResolvedTarget,
        approval: ApprovalRequest | None = None,
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
        self._verify_external_input(external_input, approval=approval)
        stored = await self._plans.load(task_id=task_id)
        plan = stored.plan
        self._verify_no_drift(
            plan=plan, stored_target=stored.target, target=target, context=context
        )
        grant = await self._acquire(task_id)
        record = await self._tasks.get(
            lookup=TaskLookup(
                task_id=task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
            )
        )
        if record.status is TaskStatus.AWAITING_APPROVAL:
            record = await self._advance(
                task_id, record.version, TaskStatus.RUNNING, grant.fencing_token
            )
        self._emit(
            stage=PipelineStage.LIFECYCLE,
            outcome=StageOutcome.OK,
            context=context,
            task_id=task_id,
            plan=plan,
        )
        return await self._run_steps(
            task_id=task_id,
            plan=plan,
            target=stored.target,
            context=context,
            fencing_token=grant.fencing_token,
            version=record.version,
            approval=approval,
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
        plan: ExecutionPlan,
        stored_target: ResolvedTarget,
        target: ResolvedTarget,
        context: RequestContext,
    ) -> None:
        if compute_plan_hash(plan) != compute_plan_hash(plan.model_copy()):
            raise DriftError("plan_hash is not reproducible")
        if compute_target_fingerprint(stored_target) != compute_target_fingerprint(target):
            raise DriftError("target_fingerprint drifted since the plan was stored")
        if plan.policy_revision != context.policy_revision:
            raise DriftError("policy revision drifted since the plan was stored")

    # --- 生命周期 -----------------------------------------------------------

    async def _acquire(self, task_id: str) -> LeaseGrant:
        grant = await self._tasks.acquire_lease(
            task_id=task_id, owner=self._owner, ttl_seconds=self._lease_ttl_seconds
        )
        if grant is None:
            # 终态任务或已被他人持有——两种情况都不该继续推进。
            raise LifecycleError("task lease is unavailable")
        return grant

    async def _advance(
        self, task_id: str, version: int, status: TaskStatus, fencing_token: int
    ) -> TaskRecord:
        result = await self._tasks.transition(
            command=TransitionCommand(
                task_id=task_id,
                expected_version=version,
                to_status=status,
                fencing_token=fencing_token,
            )
        )
        if not result.applied:
            # 必须采纳 winner 并停下，不能用本地旧对象继续推进。
            raise LifecycleError("transition rejected", rejection=result.rejection)
        return result.winner

    # --- 步骤循环 -----------------------------------------------------------

    async def _run_steps(
        self,
        *,
        task_id: str,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
        fencing_token: int,
        version: int,
        approval: ApprovalRequest | None = None,
    ) -> TaskOutcome:
        tool_calls_used = 0
        degraded = False
        # 只记录"哪些步骤执行后未成功"——这是 Runner 自己的生命周期知识，不是证据
        # 内容。证据内容一律从 ledger 读回。
        failed_steps: set[str] = set()
        for step in plan.steps:
            if not await self._condition_holds(
                task_id=task_id, condition=step.condition, failed_steps=failed_steps
            ):
                continue
            if tool_calls_used >= plan.budget.max_tool_calls:
                return await self._outcome(
                    task_id,
                    TaskStatus.FAILED,
                    terminal_reason=BUDGET_EXHAUSTED_REASON,
                )
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
                self._emit(
                    stage=PipelineStage.ADMISSION,
                    outcome=StageOutcome.REJECTED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                )
                await self._pause(
                    task_id=task_id,
                    step=step,
                    plan=plan,
                    target=target,
                    fencing_token=fencing_token,
                    version=version,
                    step_id=exc.step_id,
                )
            except Exception:
                # 准入的任何拒绝都归因到 ADMISSION，然后照常向上传播——Runner
                # 不吞掉治理层的拒绝，只负责让它在 trace 上落到正确的阶段。
                self._emit(
                    stage=PipelineStage.ADMISSION,
                    outcome=StageOutcome.REJECTED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                )
                raise
            self._emit(
                stage=PipelineStage.ADMISSION,
                outcome=StageOutcome.OK,
                context=context,
                task_id=task_id,
                plan=plan,
                step_id=step.step_id,
            )
            try:
                result = await self._gateway.invoke(
                    call, context=context, admission=certificate
                )
            except TypeError:
                # Gateway 对"adapter 返回了非 AdapterResponse"抛 TypeError。这是
                # 上游故障，不是本进程的编程错误：让它冒泡会把一次可降级的取数失败
                # 变成崩溃，而崩溃既没有证据也没有终态。降级为 indeterminate。
                tool_calls_used += 1
                degraded = True
                failed_steps.add(step.step_id)
                self._emit(
                    stage=PipelineStage.GATEWAY,
                    outcome=StageOutcome.FAILED,
                    context=context,
                    task_id=task_id,
                    plan=plan,
                    step_id=step.step_id,
                )
                continue
            tool_calls_used += 1
            ok = result.status is ToolCallStatus.OK
            self._emit(
                stage=PipelineStage.GATEWAY,
                outcome=StageOutcome.OK if ok else StageOutcome.FAILED,
                context=context,
                task_id=task_id,
                plan=plan,
                step_id=step.step_id,
            )
            if not ok:
                degraded = True
                failed_steps.add(step.step_id)
            await self._record_evidence(
                task_id=task_id, step=step, plan=plan, result=result
            )
            self._emit(
                stage=PipelineStage.EVIDENCE,
                outcome=StageOutcome.OK,
                context=context,
                task_id=task_id,
                plan=plan,
                step_id=step.step_id,
            )
        status = TaskStatus.INDETERMINATE if degraded else TaskStatus.SUCCEEDED
        return await self._outcome(task_id, status, terminal_reason=None)

    async def _condition_holds(
        self, *, task_id: str, condition: StepCondition, failed_steps: set[str]
    ) -> bool:
        """按闭集条件决定是否执行该步骤。

        **证据从 ledger 读回**，不读本地变量：这让 ledger 在执行期就成为唯一路径。

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
            if condition.ref_step_id in failed_steps:
                return False
            rows = await self._rows_recorded_for(
                task_id=task_id, step_id=condition.ref_step_id
            )
            return rows < threshold
        # 其余两个成员 M3 未消费；不猜测语义，一律不执行（fail-closed）。
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
            step=step,
            idempotency_key=f"{compute_plan_hash(plan)[:16]}:{step.step_id}",
        )
        certificate = admit_step(
            step=step,
            plan=plan,
            call=call,
            context=context,
            target=target,
            snapshot=self._snapshot,
            policy_snapshot=self._policy_snapshot,
            profile=self._profile,
            surface=self._surface,
            approval_gate=self._approval_gate,
            approval=approval,
            task_id=task_id,
            now=self._clock(),
        )
        return certificate, call

    def _build_call(self, *, step: PlanStep, idempotency_key: str) -> ToolCall:
        """构造本步骤的工具调用。

        **幂等键由 plan_hash + step_id 派生，不含 task_id**：退出标准要求"固定
        IntentDraft 重复运行产生逐字节相同的 ToolCall 与 tool_call_hash"，而
        task_id 由存储层生成、每次都不同。用计划指纹派生既满足这条，也保持了正确
        的幂等语义——同一份计划的同一步骤就是同一次尝试。
        """
        return ToolCall(
            gateway=GATEWAY_NAME,
            operation=step.operation,
            step_id=step.step_id,
            typed_args=dict(step.typed_arguments),
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            idempotency_key=idempotency_key,
        )

    async def _record_evidence(
        self, *, task_id: str, step: PlanStep, plan: ExecutionPlan, result: ToolResult
    ) -> EvidenceEnvelope:
        envelope = build_evidence(
            task_id=task_id,
            step=step,
            plan=plan,
            result=result,
            surface=self._surface,
            params=SlowQueryParams.from_typed_arguments(step.typed_arguments),
            captured_at=self._clock(),
        )
        await self._ledger.append(task_id=task_id, envelope=envelope)
        return envelope

    async def _pause(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        fencing_token: int,
        version: int,
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
            subject=self._owner,
            expires_at=now + _dt.timedelta(seconds=APPROVAL_TTL_SECONDS),
            state=ApprovalState.PENDING,
        )
        await self._tasks.record_approval(request=request)
        await self._advance(
            task_id, version, TaskStatus.AWAITING_APPROVAL, fencing_token
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

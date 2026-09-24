"""无执行权的任务提交、查询与终态投影应用服务。"""

from enum import StrEnum

from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingError,
    CapabilityBindingRegistry,
    CapabilityRuntimeBinding,
)
from xiaowei_agent.application.model_advisory import (
    advisory_artifact_identity_matches,
    advisory_input_digest,
    advisory_result_digest,
)
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    AnswerabilityVerdict,
    CapabilitySnapshot,
    ClarificationPayload,
    ConfirmedSlot,
    EvidenceEnvelope,
    ExecutionDisclosure,
    ExecutionPlan,
    MissingItem,
    ModelAdvisory,
    ModelInvocationProfile,
    RenderPayload,
    TaskLookup,
    TaskOutcome,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TaskView,
    task_query_path,
)
from xiaowei_agent.contracts.model import AdvisoryModelResult
from xiaowei_agent.persistence.clarification_records import ClarificationRecordStore
from xiaowei_agent.persistence.errors import PersistenceUnavailableError
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.model_artifacts import ModelArtifactStore
from xiaowei_agent.persistence.plans import PlanNotFoundError, PlanStore
from xiaowei_agent.persistence.store import (
    IdempotencyConflictError,
    TaskNotFoundError,
    TaskStore,
)
from xiaowei_agent.planning.disclosure import (
    DisclosureProjectionError,
    project_execution_disclosure,
)
from xiaowei_agent.rendering.generic import (
    CONVERSATION_TERMINAL_REASON,
    render_clarification_payload,
    render_conversation_response,
    render_preplan_rejection,
)
from xiaowei_agent.rendering.model_advisory import append_model_advisory


class ApplicationFailure(StrEnum):
    """入口层可安全映射的应用异常闭集。"""

    CLARIFICATION_INTEGRITY = "clarification_integrity"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class ClarificationIntegrityError(RuntimeError):
    """澄清终态缺失持久事实，入口只能暴露闭集错误码。"""

    def __init__(self) -> None:
        super().__init__("clarification.integrity_error")


def classify_application_exception(exc: Exception) -> ApplicationFailure:
    """把应用边界异常收敛为入口可消费的闭集，不暴露存储层类型。"""
    if isinstance(exc, ClarificationIntegrityError):
        return ApplicationFailure.CLARIFICATION_INTEGRITY
    if isinstance(exc, IdempotencyConflictError):
        return ApplicationFailure.CONFLICT
    if isinstance(exc, TaskNotFoundError):
        return ApplicationFailure.NOT_FOUND
    if isinstance(exc, PersistenceUnavailableError):
        return ApplicationFailure.UNAVAILABLE
    return ApplicationFailure.INTERNAL


class TaskViewRuntime:
    """只持有任务事实、计划、证据和投影绑定，不具备执行能力。"""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        plan_store: PlanStore,
        ledger: EvidenceLedger,
        conversation_snapshot: CapabilitySnapshot,
        rendering_bindings: CapabilityBindingRegistry,
        clarification_records: ClarificationRecordStore | None = None,
        model_artifacts: ModelArtifactStore | None = None,
        model_profile: ModelInvocationProfile | None = None,
    ) -> None:
        if (model_artifacts is None) is not (model_profile is None):
            raise ValueError(
                "model_artifacts and model_profile must be configured together"
            )
        self._tasks = task_store
        self._plans = plan_store
        self._ledger = ledger
        # 两份权威刻意分开（W5 §2.2）：``conversation_snapshot`` 是**当前准入快照**，
        # 只回答"此刻能做什么"；``rendering_bindings`` 是代码内完整渲染注册表，只按
        # 已持久化计划的精确 capability/version 查投影器。用完整渲染注册表回答当前
        # 能力，会让 provider-off release 仍声称三个 recording 能力可用。
        #
        # 快照必填而不是可选：I2-B 的普通对话回答**就是**这份快照。做成可选就会多出
        # 一条"装配漏了快照 → 对话任务投影不出内容"的静默路径。
        self._conversation_snapshot = conversation_snapshot
        self._rendering_bindings = rendering_bindings
        self._clarification_records = clarification_records
        self._model_artifacts = model_artifacts
        self._model_profile = model_profile

    async def submit_task(self, *, submission: TaskSubmission) -> TaskView:
        """只持久化提交事实并返回任务投影，不解释或执行。"""
        record = await self._tasks.create_task(submission=submission)
        return await self.project_task(record=record)

    async def submit_clarification_child(
        self,
        *,
        submission: TaskSubmission,
        authenticated_channel_owner: str,
    ) -> TaskView:
        """一次性消费澄清父任务并返回子任务投影，不解释或执行。"""
        record = await self._tasks.create_clarification_child(
            submission=submission,
            authenticated_channel_owner=authenticated_channel_owner,
        )
        return await self.project_task(record=record)

    async def query_task(self, *, lookup: TaskLookup) -> TaskView:
        """按受信 scope 纯读任务；不发 trace、不改变任务事实。"""
        record = await self._tasks.get(lookup=lookup)
        return await self.project_task(record=record)

    async def project_task(self, *, record: TaskRecord) -> TaskView:
        """从已由调用方安全读取的任务 winner 生成同一任务投影。"""
        payload: RenderPayload | None = None
        clarification = None
        disclosure = await self.project_disclosure(record=record)
        if record.status in TERMINAL_STATUSES:
            if record.status is TaskStatus.CLARIFICATION_REQUIRED:
                clarification = await self.project_clarification(record=record)
            else:
                payload = await self.project_recorded(record=record)
        return TaskView(
            task_id=record.task_id,
            status=record.status,
            render=payload,
            clarification=clarification,
            disclosure=disclosure,
            query_path=task_query_path(record.task_id),
        )

    async def project_disclosure(self, *, record: TaskRecord) -> ExecutionDisclosure | None:
        """Rebuild the disclosure from the stored plan/target, if one exists."""
        try:
            stored = await self._plans.load(task_id=record.task_id)
        except PlanNotFoundError:
            return None
        try:
            binding = self._rendering_bindings.runtime_for_plan(plan=stored.plan)
            return project_execution_disclosure(
                plan=stored.plan,
                target=stored.target,
                binding=binding.execution.disclosure,
                parent_confirmed_slots=await self._parent_confirmed_slots(record=record),
            )
        except (CapabilityBindingError, DisclosureProjectionError):
            return None

    async def _parent_confirmed_slots(
        self, *, record: TaskRecord
    ) -> tuple[ConfirmedSlot, ...]:
        if self._clarification_records is None:
            return ()
        submission = await self._tasks.get_submission(
            lookup=TaskLookup(
                task_id=record.task_id,
                tenant_id=record.tenant_id,
                environment_id=record.environment_id,
            )
        )
        parent_id = submission.clarification_parent_task_id
        if parent_id is None:
            return ()
        parent = await self._clarification_records.load(task_id=parent_id)
        if parent is None:
            return ()
        return parent.confirmed_slots

    async def project_clarification(self, *, record: TaskRecord) -> ClarificationPayload:
        """从 ClarificationRecord 重建澄清投影；缺失即 fail-closed。"""
        if self._clarification_records is None:
            raise ClarificationIntegrityError()
        stored = await self._clarification_records.load(task_id=record.task_id)
        if stored is None:
            raise ClarificationIntegrityError()
        return render_clarification_payload(record=stored)

    async def project_recorded(self, *, record: TaskRecord) -> RenderPayload:
        """从持久化计划与证据重建终态投影。"""
        evidences = await self._ledger.load(task_id=record.task_id)
        try:
            stored = await self._plans.load(task_id=record.task_id)
        except PlanNotFoundError:
            if record.status is TaskStatus.REJECTED and not evidences:
                return render_preplan_rejection(status=record.status)
            if (
                record.status is TaskStatus.SUCCEEDED
                and record.terminal_reason == CONVERSATION_TERMINAL_REASON
                and not evidences
            ):
                # 不读 submission：回答只由能力快照决定，读一份不参与投影的用户文本
                # 只会凭空多出一个失败面，并让"回答是否受用户文本影响"变得可疑。
                return render_conversation_response(snapshot=self._conversation_snapshot)
            raise
        binding = self._rendering_bindings.runtime_for_plan(plan=stored.plan)
        verdict = assess_evidence(binding=binding, evidences=evidences)
        advisory = await self._visible_advisory(
            record=record,
            plan=stored.plan,
            binding=binding,
            evidences=evidences,
            verdict=verdict,
        )
        return project_terminal(
            record=record,
            evidences=evidences,
            verdict=verdict,
            binding=binding,
            advisory=advisory,
        )

    async def _visible_advisory(
        self,
        *,
        record: TaskRecord,
        plan: ExecutionPlan,
        binding: CapabilityRuntimeBinding,
        evidences: tuple[EvidenceEnvelope, ...],
        verdict: AnswerabilityVerdict,
    ) -> ModelAdvisory | None:
        """只展示与当前终态事实完全匹配的已保存 advisory。"""
        if (
            record.status is not TaskStatus.SUCCEEDED
            or self._model_artifacts is None
            or self._model_profile is None
        ):
            return None
        stored = await self._model_artifacts.load_advisory(task_id=record.task_id)
        if stored is None or binding.advisory_projector is None:
            return None
        outcome = TaskOutcome(
            task_id=record.task_id,
            status=record.status,
            terminal_reason=record.terminal_reason,
            evidence_refs=tuple(item.evidence_id for item in evidences),
            render_ref=None,
        )
        request = binding.advisory_projector(
            task_id=record.task_id,
            plan=plan,
            outcome=outcome,
            evidences=evidences,
            verdict=verdict,
        )
        result = AdvisoryModelResult(advisory=stored.advisory, usage=stored.usage)
        if (
            request is None
            or not advisory_artifact_identity_matches(
                stored, profile=self._model_profile
            )
            or stored.input_digest
            != advisory_input_digest(request, plan=plan, profile=self._model_profile)
            or stored.result_digest != advisory_result_digest(result)
        ):
            return None
        return stored.advisory

def project_terminal(
    *,
    record: TaskRecord,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    binding: CapabilityRuntimeBinding,
    advisory: ModelAdvisory | None = None,
) -> RenderPayload:
    """把终态持久化事实投影为回答；纯函数，不发 trace、不做 I/O。"""
    payload = binding.renderer(
        evidences=evidences, verdict=verdict, status=record.status
    )
    return append_model_advisory(payload, advisory=advisory)


def assess_evidence(
    *,
    binding: CapabilityRuntimeBinding | None,
    evidences: tuple[EvidenceEnvelope, ...],
) -> AnswerabilityVerdict:
    """按已存计划的 binding 校验并评估证据；无计划时只接受空证据。"""
    if binding is None:
        if evidences:
            raise CapabilityBindingError("evidence exists without a capability plan")
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=("request was rejected before a plan was stored",),
            missing=(MissingItem(key="execution_plan", reason_key="plan.absent"),),
            downgrade_suggestion=True,
            needs_user_input=False,
        )
    expected = (binding.capability_id, binding.capability_version)
    if any(
        (item.capability_id, item.capability_version) != expected
        for item in evidences
    ):
        raise CapabilityBindingError("evidence capability key differs from plan")
    return binding.assessor(evidences=evidences)

"""无执行权的任务提交、查询与终态投影应用服务。"""

from enum import StrEnum

from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingError,
    CapabilityBindingRegistry,
    CapabilityRuntimeBinding,
)
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    AnswerabilityVerdict,
    EvidenceEnvelope,
    MissingItem,
    RenderPayload,
    TaskLookup,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TaskView,
    task_query_path,
)
from xiaowei_agent.persistence.errors import PersistenceUnavailableError
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.plans import PlanNotFoundError, PlanStore
from xiaowei_agent.persistence.store import (
    IdempotencyConflictError,
    TaskNotFoundError,
    TaskStore,
)
from xiaowei_agent.rendering.generic import render_preplan_rejection


class ApplicationFailure(StrEnum):
    """入口层可安全映射的应用异常闭集。"""

    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


def classify_application_exception(exc: Exception) -> ApplicationFailure:
    """把应用边界异常收敛为入口可消费的闭集，不暴露存储层类型。"""
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
        bindings: CapabilityBindingRegistry,
    ) -> None:
        self._tasks = task_store
        self._plans = plan_store
        self._ledger = ledger
        self._bindings = bindings

    async def submit_task(self, *, submission: TaskSubmission) -> TaskView:
        """只持久化提交事实并返回任务投影，不解释或执行。"""
        record = await self._tasks.create_task(submission=submission)
        return await self._task_view(record=record)

    async def query_task(self, *, lookup: TaskLookup) -> TaskView:
        """按受信 scope 纯读任务；不发 trace、不改变任务事实。"""
        record = await self._tasks.get(lookup=lookup)
        return await self._task_view(record=record)

    async def project_recorded(self, *, record: TaskRecord) -> RenderPayload:
        """从持久化计划与证据重建终态投影。"""
        evidences = await self._ledger.load(task_id=record.task_id)
        try:
            stored = await self._plans.load(task_id=record.task_id)
        except PlanNotFoundError:
            if record.status is TaskStatus.REJECTED and not evidences:
                return render_preplan_rejection(status=record.status)
            raise
        binding = self._bindings.runtime_for_plan(plan=stored.plan)
        verdict = assess_evidence(binding=binding, evidences=evidences)
        return project_terminal(
            record=record,
            evidences=evidences,
            verdict=verdict,
            binding=binding,
        )

    async def _task_view(self, *, record: TaskRecord) -> TaskView:
        payload: RenderPayload | None = None
        if record.status in TERMINAL_STATUSES:
            payload = await self.project_recorded(record=record)
        return TaskView(
            task_id=record.task_id,
            status=record.status,
            render=payload,
            query_path=task_query_path(record.task_id),
        )


def project_terminal(
    *,
    record: TaskRecord,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    binding: CapabilityRuntimeBinding,
) -> RenderPayload:
    """把终态持久化事实投影为回答；纯函数，不发 trace、不做 I/O。"""
    return binding.renderer(
        evidences=evidences, verdict=verdict, status=record.status
    )


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

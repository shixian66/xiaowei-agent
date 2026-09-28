"""F1 只读查询的 Params、SlotVerifier 与 PlanCompiler（设计 §5.4、§5.5）。

SQL 来源只有两处受信事实：``ArtifactSubmission`` 的引用，或目标选择澄清记录里保存的引用。
模型或关键词规则给出的同名意图没有这两者，永远得不到可执行参数。计划只保存引用、hash、
目标与预算，不含 SQL 原文。
"""

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar, Final

from pydantic import Field

from xiaowei_agent.capabilities.effect import build_plan_step
from xiaowei_agent.capabilities.readonly_query import (
    OP_EXECUTE_READONLY_QUERY,
    READONLY_QUERY_CAPABILITY_ID,
    READONLY_QUERY_CAPABILITY_VERSION,
    READONLY_QUERY_INPUT_SCHEMA_REF,
    ReadonlyQueryTarget,
    ReadonlyQueryTargetCatalog,
)
from xiaowei_agent.contracts import (
    Candidate,
    CapabilityParams,
    CapabilitySnapshot,
    CapabilitySubject,
    ClarificationContext,
    ClarificationReasonCode,
    ExecutionPlan,
    IntentDraft,
    InteractionRejectionReasonCode,
    PlanBudget,
    RequestContext,
    ResolvedTarget,
    Sha256Hex,
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
    SlotVerificationResult,
    StrictStr,
)
from xiaowei_agent.contracts.clarification import TargetOption, TargetSelection
from xiaowei_agent.contracts.intent import READONLY_QUERY_INTENT
from xiaowei_agent.contracts.sql_query import (
    ReadonlyQueryBudget,
    SqlArtifactRef,
    confirmed_artifact_arguments,
)
from xiaowei_agent.planning.canonical import compute_target_fingerprint

READONLY_QUERY_PLAN_BUDGET: Final[PlanBudget] = PlanBudget(
    max_steps=1, max_tool_calls=1, max_model_tokens=0
)
READONLY_QUERY_STEP_ID: Final[str] = "s1"
READONLY_QUERY_PROVIDER: Final[str] = "starrocks"
READONLY_QUERY_RESOURCE_KIND: Final[str] = "cluster"
READONLY_QUERY_SELECTOR_VERSION: Final[str] = "f1.readonly_query.v1"


class ReadonlyQueryParams(CapabilityParams):
    """一次只读查询的全部受信输入：SQL 引用、唯一目标、config revision 与预算。"""

    INPUT_SCHEMA_REF: ClassVar[str] = READONLY_QUERY_INPUT_SCHEMA_REF

    sql_ref: StrictStr = Field(min_length=1)
    sql_hash: Sha256Hex
    resource_id: StrictStr = Field(min_length=1)
    config_revision: Sha256Hex
    budget: ReadonlyQueryBudget


def _invalid(reason: InteractionRejectionReasonCode) -> SlotInvalid:
    return SlotInvalid(reason_code=reason)


def _options(targets: tuple[ReadonlyQueryTarget, ...]) -> tuple[TargetOption, ...]:
    return tuple(
        TargetOption(resource_id=target.resource_id, display_name=target.display_name)
        for target in targets
    )


def _ready(
    artifact: SqlArtifactRef, target: ReadonlyQueryTarget
) -> SlotReady[ReadonlyQueryParams]:
    return SlotReady(
        params=ReadonlyQueryParams(
            sql_ref=artifact.sql_ref,
            sql_hash=artifact.sql_hash,
            resource_id=target.resource_id,
            config_revision=target.config_revision,
            budget=target.budget,
        ),
        confirmed_slots=(),
    )


def _ask(selection: TargetSelection) -> SlotIncomplete:
    return SlotIncomplete(
        reason_code=ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED,
        missing_fields=(),
        confirmed_slots=(),
        target_selection=selection,
    )


@dataclass(frozen=True)
class ReadonlyQuerySlotVerifier:
    """按设计 §5.4 决策表确定唯一目标；不猜、不取默认、不做模糊匹配。"""

    catalog: ReadonlyQueryTargetCatalog

    def __call__(
        self,
        *,
        candidate: Candidate,
        draft: IntentDraft,
        context: RequestContext,
        as_of: dt.datetime,
        user_text: str,
        clarification: ClarificationContext | None = None,
        sql_artifact: SqlArtifactRef | None = None,
    ) -> SlotVerificationResult[ReadonlyQueryParams]:
        _ = as_of
        if (
            candidate.capability_id != READONLY_QUERY_CAPABILITY_ID
            or candidate.capability_version != READONLY_QUERY_CAPABILITY_VERSION
            or candidate.operation != OP_EXECUTE_READONLY_QUERY
            or draft.intent != READONLY_QUERY_INTENT
            or draft.slots
        ):
            return _invalid(InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID)
        targets = self.catalog.targets_for(
            tenant_id=context.tenant_id, environment_id=context.environment_id
        )
        if clarification is None:
            if sql_artifact is None:
                return _invalid(InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID)
            if not targets:
                return _invalid(InteractionRejectionReasonCode.CAPABILITY_TARGET_UNAVAILABLE)
            if len(targets) == 1:
                return _ready(sql_artifact, targets[0])
            return _ask(
                TargetSelection(
                    sql_ref=sql_artifact.sql_ref,
                    sql_hash=sql_artifact.sql_hash,
                    options=_options(targets),
                )
            )
        subject = clarification.subject
        if (
            sql_artifact is not None
            or not isinstance(subject, CapabilitySubject)
            or subject.target_selection is None
        ):
            return _invalid(InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID)
        selection = subject.target_selection
        if _options(targets) != selection.options:
            # 追问之后目标集合变了（重启加载了新配置）：回答所指已不可信。
            return _invalid(InteractionRejectionReasonCode.CAPABILITY_TARGET_UNAVAILABLE)
        chosen = next(
            (target for target in targets if target.display_name == user_text), None
        )
        if chosen is None:
            return _ask(selection)
        return _ready(
            SqlArtifactRef(sql_ref=selection.sql_ref, sql_hash=selection.sql_hash),
            chosen,
        )


def resolve_readonly_query_target(
    *, context: RequestContext, params: ReadonlyQueryParams
) -> ResolvedTarget:
    """目标 = 受信上下文的租户与环境 + SlotVerifier 选定的唯一资源。"""
    return ResolvedTarget(
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
        provider=READONLY_QUERY_PROVIDER,
        resource_kind=READONLY_QUERY_RESOURCE_KIND,
        resource_ids=(params.resource_id,),
        selector_version=READONLY_QUERY_SELECTOR_VERSION,
    )


def compile_readonly_query_plan(
    *,
    candidate: Candidate,
    params: ReadonlyQueryParams,
    target: ResolvedTarget,
    context: RequestContext,
    snapshot: CapabilitySnapshot,
) -> ExecutionPlan:
    """编译唯一步骤；typed_arguments 只有引用、hash、目标指纹、revision 与预算。

    :raises ValueError: 候选不是 F1 入口、目标不是规范解析结果，或能力不在快照中。
    """
    if (
        candidate.capability_id != READONLY_QUERY_CAPABILITY_ID
        or candidate.capability_version != READONLY_QUERY_CAPABILITY_VERSION
        or candidate.operation != OP_EXECUTE_READONLY_QUERY
    ):
        raise ValueError("candidate does not identify the readonly query entry")
    if target != resolve_readonly_query_target(context=context, params=params):
        raise ValueError("target differs from canonical resolution")
    spec = next(
        (
            item
            for item in snapshot.specs
            if item.capability_id == candidate.capability_id
            and item.version == candidate.capability_version
        ),
        None,
    )
    if spec is None:
        raise ValueError("candidate capability is absent from the snapshot")
    step = build_plan_step(
        snapshot,
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        operation=OP_EXECUTE_READONLY_QUERY,
        step_id=READONLY_QUERY_STEP_ID,
        typed_arguments=confirmed_artifact_arguments(
            sql_ref=params.sql_ref,
            sql_hash=params.sql_hash,
            resource_id=params.resource_id,
            target_fingerprint=compute_target_fingerprint(target),
            config_revision=params.config_revision,
            budget=params.budget,
        ),
    )
    return ExecutionPlan(
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        steps=(step,),
        policy_profile=spec.policy_profile,
        policy_revision=context.policy_revision,
        budget=READONLY_QUERY_PLAN_BUDGET,
    )

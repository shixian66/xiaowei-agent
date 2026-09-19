"""Test-only capability bindings；合成写能力永不进入生产装配。"""

import datetime as dt
from dataclasses import replace
from typing import ClassVar

from tests.fakes.admission import WRITE_PROFILE
from tests.fakes.fixtures import CAP_VERSION, WRITE_CAP, WRITE_OP

from xiaowei_agent.application.capability_input import SlotInvalid, bind_capability_input
from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingRegistry,
    CapabilityRuntimeBinding,
    PreparedCapability,
)
from xiaowei_agent.application.default_capabilities import (
    ASSET_INVENTORY_BINDING,
    PROMETHEUS_ALERT_BINDING,
    SLOW_QUERY_BINDING,
)
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    Candidate,
    CapabilityParams,
    CapabilitySnapshot,
    ClarificationContext,
    EffectClass,
    EvidenceEnvelope,
    ExecutionPlan,
    IntentDraft,
    InteractionRejectionReasonCode,
    MissingItem,
    PlanStep,
    PolicySnapshot,
    RenderPayload,
    RequestContext,
    ResolvedTarget,
    TaskStatus,
    ToolResult,
)
from xiaowei_agent.planning.disclosure import DisclosureProjectionBinding
from xiaowei_agent.runners.binding import CapabilityExecutionBinding


class _SyntheticWriteParams(CapabilityParams):
    INPUT_SCHEMA_REF: ClassVar[str] = "input.synthetic.write.v1"


def _never_verify_slots(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    user_text: str,
    clarification: ClarificationContext | None = None,
) -> SlotInvalid:
    _ = (candidate, draft, context, as_of, user_text, clarification)
    return SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID
    )


def _never_plan_typed_input(
    *,
    candidate: Candidate,
    params: _SyntheticWriteParams,
    context: RequestContext,
    snapshot: CapabilitySnapshot,
) -> PreparedCapability:
    _ = (candidate, params, context, snapshot)
    raise AssertionError("synthetic capability is never planned by Runtime")


def _never_build_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    raise AssertionError("synthetic write must stop before Gateway")


def _assess_synthetic(
    *, evidences: tuple[EvidenceEnvelope, ...]
) -> AnswerabilityVerdict:
    return AnswerabilityVerdict(
        sufficient=False,
        limitations=("synthetic write is test-only",),
        missing=(MissingItem(key="approval", reason_key="approval.required"),),
        downgrade_suggestion=True,
        needs_user_input=False,
    )


def _never_render(
    *,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    status: TaskStatus,
) -> RenderPayload:
    raise AssertionError("synthetic capability has no production renderer")


SYNTHETIC_WRITE_BINDING = CapabilityRuntimeBinding(
    capability_id=WRITE_CAP,
    capability_version=CAP_VERSION,
    entry_operation=WRITE_OP,
    input_binding=bind_capability_input(
        params_type=_SyntheticWriteParams,
        input_schema_ref=_SyntheticWriteParams.INPUT_SCHEMA_REF,
        allowed_clarification_fields=frozenset(),
        slot_verifier=_never_verify_slots,
        planner=_never_plan_typed_input,
        confirmed_slot_projector=None,
    ),
    assessor=_assess_synthetic,
    renderer=_never_render,
    execution=CapabilityExecutionBinding(
        capability_id=WRITE_CAP,
        capability_version=CAP_VERSION,
        disclosure=DisclosureProjectionBinding(
            capability_id=WRITE_CAP,
            capability_version=CAP_VERSION,
            allowed_clarification_fields=frozenset(),
            confirmed_slot_projector=None,
        ),
        policy_profile=WRITE_PROFILE,
        sql_surface=None,
        promql_surface=None,
        evidence_builder=_never_build_evidence,
    ),
)


def build_test_capability_bindings(
    *, snapshot: CapabilitySnapshot, policy_snapshot: PolicySnapshot
) -> CapabilityBindingRegistry:
    """按测试 snapshot 装配；合成写 binding 仅从本模块可见。"""
    specs = {(spec.capability_id, spec.version): spec for spec in snapshot.specs}
    keys = set(specs)
    slow_spec = specs[(SLOW_QUERY_BINDING.capability_id, SLOW_QUERY_BINDING.capability_version)]
    slow_binding = SLOW_QUERY_BINDING
    if slow_spec.policy_profile != SLOW_QUERY_BINDING.execution.policy_profile.profile_id:
        profile = SLOW_QUERY_BINDING.execution.policy_profile.model_copy(
            update={
                "profile_id": slow_spec.policy_profile,
                "allowed_operations": tuple(
                    operation.operation for operation in slow_spec.operations
                ),
                "allowed_effect_classes": (EffectClass.READ,),
            }
        )
        slow_binding = replace(
            SLOW_QUERY_BINDING,
            execution=replace(SLOW_QUERY_BINDING.execution, policy_profile=profile),
        )
    bindings = [slow_binding]
    prometheus_key = (
        PROMETHEUS_ALERT_BINDING.capability_id,
        PROMETHEUS_ALERT_BINDING.capability_version,
    )
    if prometheus_key in keys:
        bindings.append(PROMETHEUS_ALERT_BINDING)
    asset_key = (
        ASSET_INVENTORY_BINDING.capability_id,
        ASSET_INVENTORY_BINDING.capability_version,
    )
    if asset_key in keys:
        bindings.append(ASSET_INVENTORY_BINDING)
    if (WRITE_CAP, CAP_VERSION) in keys:
        bindings.append(SYNTHETIC_WRITE_BINDING)
    return CapabilityBindingRegistry(
        snapshot=snapshot,
        policy_snapshot=policy_snapshot,
        bindings=bindings,
    )

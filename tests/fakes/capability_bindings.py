"""Test-only capability bindings；合成写能力永不进入生产装配。"""

import datetime as dt
from dataclasses import replace

from tests.fakes.admission import WRITE_PROFILE
from tests.fakes.fixtures import CAP_VERSION, WRITE_CAP, WRITE_OP

from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingRegistry,
    CapabilityRuntimeBinding,
    PreparedCapability,
)
from xiaowei_agent.application.default_capabilities import SLOW_QUERY_BINDING
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    Candidate,
    CapabilitySnapshot,
    EffectClass,
    EvidenceEnvelope,
    ExecutionPlan,
    IntentDraft,
    MissingItem,
    PlanStep,
    PolicySnapshot,
    RenderPayload,
    RequestContext,
    TaskStatus,
    ToolResult,
)
from xiaowei_agent.runners.binding import CapabilityExecutionBinding


def _never_prepare(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    snapshot: CapabilitySnapshot,
) -> PreparedCapability:
    raise AssertionError("synthetic capability is never planned by Runtime")


def _never_build_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
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
    planner=_never_prepare,
    assessor=_assess_synthetic,
    renderer=_never_render,
    execution=CapabilityExecutionBinding(
        capability_id=WRITE_CAP,
        capability_version=CAP_VERSION,
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
    if (WRITE_CAP, CAP_VERSION) in keys:
        bindings.append(SYNTHETIC_WRITE_BINDING)
    return CapabilityBindingRegistry(
        snapshot=snapshot,
        policy_snapshot=policy_snapshot,
        bindings=bindings,
    )

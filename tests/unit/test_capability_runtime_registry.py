"""Capability binding 只做精确装配，不成为第二个候选解析器。"""

import datetime as dt
from dataclasses import FrozenInstanceError

import pytest
from tests.fakes.admission import CONTEXT, DRAFT, POLICY_SNAPSHOT, slow_query_plan

from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingError,
    CapabilityBindingRegistry,
    CapabilityRuntimeBinding,
    PreparedCapability,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import CAPABILITY_ID, OP_COUNT, OP_LIST
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    Candidate,
    CandidateSet,
    CapabilitySnapshot,
    EvidenceEnvelope,
    ExecutionPlan,
    IntentDraft,
    PolicyProfile,
    PolicySnapshot,
    RenderPayload,
    RequestContext,
    TaskStatus,
    ToolResult,
)
from xiaowei_agent.governance.profiles import SLOW_QUERY_READONLY_PROFILE
from xiaowei_agent.runners.binding import CapabilityExecutionBinding


def _single_capability_snapshot() -> CapabilitySnapshot:
    snapshot = StaticCapabilityRegistry().snapshot()
    slow_query = next(
        spec for spec in snapshot.specs if spec.capability_id == CAPABILITY_ID
    )
    return CapabilitySnapshot(snapshot_id=snapshot.snapshot_id, specs=(slow_query,))


def _never_prepare(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    snapshot: CapabilitySnapshot,
) -> PreparedCapability:
    raise AssertionError("planner must not run while validating bindings")


def _never_assess(
    *, evidences: tuple[EvidenceEnvelope, ...]
) -> AnswerabilityVerdict:
    raise AssertionError("assessor must not run while validating bindings")


def _never_render(
    *,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    status: TaskStatus,
) -> RenderPayload:
    raise AssertionError("renderer must not run while validating bindings")


def _never_build_evidence(
    *,
    task_id: str,
    step: object,
    plan: ExecutionPlan,
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    raise AssertionError("builder must not run while validating bindings")


def _binding(
    *,
    capability_id: str = CAPABILITY_ID,
    capability_version: str = "1.0.0",
    entry_operation: str = OP_LIST,
    profile: PolicyProfile = SLOW_QUERY_READONLY_PROFILE,
    execution_capability_id: str | None = None,
) -> CapabilityRuntimeBinding:
    return CapabilityRuntimeBinding(
        capability_id=capability_id,
        capability_version=capability_version,
        entry_operation=entry_operation,
        planner=_never_prepare,
        assessor=_never_assess,
        renderer=_never_render,
        execution=CapabilityExecutionBinding(
            capability_id=(
                capability_id
                if execution_capability_id is None
                else execution_capability_id
            ),
            capability_version=capability_version,
            policy_profile=profile,
            sql_surface=None,
            promql_surface=None,
            evidence_builder=_never_build_evidence,
        ),
    )


def _candidate_set(*items: Candidate, snapshot_id: str | None = None) -> CandidateSet:
    snapshot = _single_capability_snapshot()
    return CandidateSet(
        resolver_version="resolver.test.v1",
        snapshot_id=snapshot.snapshot_id if snapshot_id is None else snapshot_id,
        items=items,
        rejections=(),
    )


def _resolved_candidates() -> CandidateSet:
    snapshot = _single_capability_snapshot()
    return DeterministicCapabilityResolver().resolve(
        draft=DRAFT, context=CONTEXT, snapshot=snapshot
    )


def _registry(*bindings: CapabilityRuntimeBinding) -> CapabilityBindingRegistry:
    return CapabilityBindingRegistry(
        snapshot=_single_capability_snapshot(),
        policy_snapshot=POLICY_SNAPSHOT,
        bindings=bindings or (_binding(),),
    )


def test_registry_selects_only_the_declared_entry_candidate() -> None:
    binding = _binding()
    candidate, selected = _registry(binding).select_entry(_resolved_candidates())
    assert candidate.operation == OP_LIST
    assert selected is binding


@pytest.mark.parametrize(
    "bindings",
    [
        (),
        (_binding(), _binding()),
        (_binding(capability_id="test.extra"),),
    ],
    ids=["missing", "duplicate", "extra"],
)
def test_registry_rejects_a_binding_set_that_differs_from_the_snapshot(
    bindings: tuple[CapabilityRuntimeBinding, ...],
) -> None:
    with pytest.raises(CapabilityBindingError):
        CapabilityBindingRegistry(
            snapshot=_single_capability_snapshot(),
            policy_snapshot=POLICY_SNAPSHOT,
            bindings=bindings,
        )


def test_registry_rejects_an_undeclared_entry_operation() -> None:
    with pytest.raises(CapabilityBindingError):
        _registry(_binding(entry_operation="undeclared"))


def test_registry_rejects_a_profile_that_differs_from_the_capability_spec() -> None:
    profile = SLOW_QUERY_READONLY_PROFILE.model_copy(
        update={"profile_id": "readonly.wrong"}
    )
    with pytest.raises(CapabilityBindingError):
        _registry(_binding(profile=profile))


def test_registry_rejects_a_profile_absent_from_the_active_policy_snapshot() -> None:
    with pytest.raises(CapabilityBindingError):
        CapabilityBindingRegistry(
            snapshot=_single_capability_snapshot(),
            policy_snapshot=PolicySnapshot(
                policy_revision=POLICY_SNAPSHOT.policy_revision,
                profiles=(),
            ),
            bindings=(_binding(),),
        )


def test_registry_rejects_an_execution_binding_for_another_capability() -> None:
    with pytest.raises(CapabilityBindingError):
        _registry(_binding(execution_capability_id="test.other"))


def test_selection_rejects_candidates_from_more_than_one_capability() -> None:
    resolved = _resolved_candidates()
    foreign = resolved.items[0].model_copy(
        update={"capability_id": "test.other"}
    )
    with pytest.raises(CapabilityBindingError):
        _registry().select_entry(_candidate_set(*resolved.items, foreign))


def test_selection_rejects_a_set_without_the_declared_entry_operation() -> None:
    only_non_entry = next(
        item for item in _resolved_candidates().items if item.operation == OP_COUNT
    )
    with pytest.raises(CapabilityBindingError):
        _registry().select_entry(_candidate_set(only_non_entry))


def test_selection_rejects_a_non_exact_capability_version() -> None:
    foreign_version = _resolved_candidates().items[0].model_copy(
        update={"capability_version": "9.9.9"}
    )
    with pytest.raises(CapabilityBindingError):
        _registry().select_entry(_candidate_set(foreign_version))


def test_selection_rejects_a_candidate_set_from_another_snapshot() -> None:
    with pytest.raises(CapabilityBindingError):
        _registry().select_entry(
            _candidate_set(*_resolved_candidates().items, snapshot_id="snapshot.other")
        )


def test_execution_lookup_rejects_a_plan_profile_mismatch() -> None:
    plan = slow_query_plan().model_copy(update={"policy_profile": "readonly.wrong"})
    with pytest.raises(CapabilityBindingError):
        _registry().execution_for(plan=plan)


def test_runtime_binding_is_frozen() -> None:
    binding = _binding()
    with pytest.raises(FrozenInstanceError):
        binding.capability_id = "test.other"  # type: ignore[misc]

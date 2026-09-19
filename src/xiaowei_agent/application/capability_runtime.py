"""显式 capability binding；只消费 Resolver 的候选，不生成候选。"""

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from xiaowei_agent.application.capability_input import CapabilityInputBinding
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    Candidate,
    CandidateSet,
    CapabilitySnapshot,
    EvidenceEnvelope,
    ExecutionPlan,
    PolicySnapshot,
    RenderPayload,
    ResolvedTarget,
    SlowQueryAdvisoryRequest,
    TaskOutcome,
    TaskStatus,
)
from xiaowei_agent.runners.binding import CapabilityExecutionBinding

CapabilityKey = tuple[str, str]


class CapabilityBindingError(RuntimeError):
    """binding 与版本化声明、策略或候选集合不一致。"""


class CapabilityPreparationError(RuntimeError):
    """领域 planner 无法从受限输入确定目标或计划。"""


@dataclass(frozen=True)
class PreparedCapability:
    """一个 binding 确定性解析出的目标和计划。"""

    target: ResolvedTarget
    plan: ExecutionPlan


class CapabilityAssessor(Protocol):
    def __call__(
        self, *, evidences: tuple[EvidenceEnvelope, ...]
    ) -> AnswerabilityVerdict: ...


class CapabilityRenderer(Protocol):
    def __call__(
        self,
        *,
        evidences: tuple[EvidenceEnvelope, ...],
        verdict: AnswerabilityVerdict,
        status: TaskStatus,
    ) -> RenderPayload: ...


class CapabilityAdvisoryProjector(Protocol):
    """把 capability 证据收窄成无执行权的模型请求；不满足条件时拒绝。"""

    def __call__(
        self,
        *,
        task_id: str,
        plan: ExecutionPlan,
        outcome: TaskOutcome,
        evidences: tuple[EvidenceEnvelope, ...],
        verdict: AnswerabilityVerdict,
    ) -> SlowQueryAdvisoryRequest | None: ...


@dataclass(frozen=True)
class CapabilityRuntimeBinding:
    """应用编排与 Runner 执行共同使用的一项显式能力装配。"""

    capability_id: str
    capability_version: str
    entry_operation: str
    input_binding: CapabilityInputBinding[Any]
    assessor: CapabilityAssessor
    renderer: CapabilityRenderer
    execution: CapabilityExecutionBinding
    advisory_projector: CapabilityAdvisoryProjector | None = None


class CapabilityBindingRegistry:
    """校验完整装配，并按精确版本 key 查找 binding。"""

    def __init__(
        self,
        *,
        snapshot: CapabilitySnapshot,
        policy_snapshot: PolicySnapshot,
        bindings: Iterable[CapabilityRuntimeBinding],
    ) -> None:
        materialized = tuple(bindings)
        keys = tuple(_binding_key(binding) for binding in materialized)
        if len(set(keys)) != len(keys):
            raise CapabilityBindingError("duplicate capability binding")

        expected = {(spec.capability_id, spec.version) for spec in snapshot.specs}
        if set(keys) != expected:
            raise CapabilityBindingError("binding keys differ from capability snapshot")

        specs = {
            (spec.capability_id, spec.version): spec for spec in snapshot.specs
        }
        for binding in materialized:
            key = _binding_key(binding)
            spec = specs[key]
            if binding.input_binding.input_schema_ref != spec.input_schema_ref:
                raise CapabilityBindingError("input binding schema differs from spec")
            operations = {operation.operation for operation in spec.operations}
            if binding.entry_operation not in operations:
                raise CapabilityBindingError("entry operation is not declared")
            execution = binding.execution
            if (execution.capability_id, execution.capability_version) != key:
                raise CapabilityBindingError("execution binding key differs")
            if (
                execution.disclosure.capability_id,
                execution.disclosure.capability_version,
            ) != key:
                raise CapabilityBindingError("disclosure binding key differs")
            if (
                execution.disclosure.allowed_clarification_fields
                != binding.input_binding.allowed_clarification_fields
            ):
                raise CapabilityBindingError("disclosure slot fields differ")
            if (
                execution.disclosure.confirmed_slot_projector
                is not binding.input_binding.confirmed_slot_projector
            ):
                raise CapabilityBindingError("disclosure projector differs")
            if execution.policy_profile.profile_id != spec.policy_profile:
                raise CapabilityBindingError("binding policy profile differs from spec")
            if execution.policy_profile.profile_id not in policy_snapshot.profiles:
                raise CapabilityBindingError("binding policy profile is not active")

        self._snapshot_id = snapshot.snapshot_id
        self._operations = MappingProxyType(
            {
                key: frozenset(
                    operation.operation for operation in specs[key].operations
                )
                for key in keys
            }
        )
        self._bindings = MappingProxyType(dict(zip(keys, materialized, strict=True)))

    def select_entry(
        self, candidates: CandidateSet
    ) -> tuple[Candidate, CapabilityRuntimeBinding]:
        """选择候选集合中唯一 capability 的声明入口；不排序、不回退。"""
        if candidates.snapshot_id != self._snapshot_id:
            raise CapabilityBindingError("candidate snapshot differs")
        keys = {
            (item.capability_id, item.capability_version)
            for item in candidates.items
        }
        if len(keys) != 1:
            raise CapabilityBindingError("candidates do not identify one capability")
        key = next(iter(keys))
        binding = self._bindings.get(key)
        if binding is None:
            raise CapabilityBindingError("candidate has no exact binding")
        if any(item.operation not in self._operations[key] for item in candidates.items):
            raise CapabilityBindingError("candidate operation is not declared")
        entry = next(
            (
                item
                for item in candidates.items
                if item.operation == binding.entry_operation
            ),
            None,
        )
        if entry is None:
            raise CapabilityBindingError("entry candidate is absent")
        return entry, binding

    def runtime_for_plan(self, *, plan: ExecutionPlan) -> CapabilityRuntimeBinding:
        """按已存计划精确选择投影 binding，并复核 profile。"""
        key = (plan.capability_id, plan.capability_version)
        binding = self._bindings.get(key)
        if binding is None:
            raise CapabilityBindingError("plan has no exact binding")
        if plan.policy_profile != binding.execution.policy_profile.profile_id:
            raise CapabilityBindingError("plan policy profile differs from binding")
        return binding

    def execution_for(self, *, plan: ExecutionPlan) -> CapabilityExecutionBinding:
        """实现 Runner 的窄 ``ExecutionBindingProvider``。"""
        return self.runtime_for_plan(plan=plan).execution


def _binding_key(binding: CapabilityRuntimeBinding) -> CapabilityKey:
    return binding.capability_id, binding.capability_version

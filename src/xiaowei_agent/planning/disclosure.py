"""Pure ExecutionDisclosure projection from stored plan facts."""

from dataclasses import dataclass
from typing import Protocol

from xiaowei_agent.contracts import (
    ClarificationField,
    ConfirmedSlot,
    EffectClass,
    ExecutionDisclosure,
    ExecutionDisclosureDisposition,
    ExecutionDisclosureSlotChange,
    ExecutionDisclosureStep,
    ExecutionPlan,
    ReadClass,
    ResolvedTarget,
)
from xiaowei_agent.planning.slot_verification import (
    SlotVerificationError,
    validate_confirmed_snapshot,
)


class ConfirmedSlotProjector(Protocol):
    def __call__(
        self, *, plan: ExecutionPlan, target: ResolvedTarget
    ) -> tuple[ConfirmedSlot, ...]: ...


class DisclosureProjectionError(RuntimeError):
    """Disclosure cannot be safely reconstructed from current plan facts."""

    reason_code = "disclosure.projection_failed"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class DisclosureProjectionBinding:
    capability_id: str
    capability_version: str
    allowed_clarification_fields: frozenset[ClarificationField]
    confirmed_slot_projector: ConfirmedSlotProjector | None

    def __post_init__(self) -> None:
        if any(
            not isinstance(field, ClarificationField)
            for field in self.allowed_clarification_fields
        ):
            raise DisclosureProjectionError
        if self.confirmed_slot_projector is None:
            return
        if not callable(self.confirmed_slot_projector):
            raise DisclosureProjectionError


def project_execution_disclosure(
    *,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    binding: DisclosureProjectionBinding,
    parent_confirmed_slots: tuple[ConfirmedSlot, ...] = (),
) -> ExecutionDisclosure:
    """Build a disclosure from plan/target and the exact current binding."""
    try:
        _require_binding_matches_plan(plan=plan, binding=binding)
        confirmed_slots = _project_confirmed_slots(
            plan=plan, target=target, binding=binding
        )
        parent_slots = validate_confirmed_snapshot(
            parent_confirmed_slots,
            allowed_fields=binding.allowed_clarification_fields,
        )
        has_side_effect = any(
            step.side_effect or step.effect_class is not EffectClass.READ
            for step in plan.steps
        )
        read_classes = tuple(
            sorted(
                {step.read_class for step in plan.steps if step.read_class is not None},
                key=lambda item: item.value,
            )
        )
        return ExecutionDisclosure(
            capability_id=plan.capability_id,
            capability_version=plan.capability_version,
            environment_id=target.environment_id,
            provider=target.provider,
            resource_kind=target.resource_kind,
            resource_ids=target.resource_ids,
            pure_read_only=not has_side_effect,
            plan_disposition=_plan_disposition(
                has_side_effect=has_side_effect,
                read_classes=read_classes,
            ),
            read_classes=read_classes,
            has_side_effect=has_side_effect,
            steps=tuple(
                ExecutionDisclosureStep(
                    step_id=step.step_id,
                    operation=step.operation,
                    effect_class=step.effect_class,
                    read_class=step.read_class,
                    side_effect=step.side_effect,
                )
                for step in plan.steps
            ),
            confirmed_slots=confirmed_slots,
            parent_changes=_slot_changes(parent=parent_slots, current=confirmed_slots),
            external_target_access=True,
        )
    except DisclosureProjectionError:
        raise
    except (SlotVerificationError, ValueError, TypeError):
        raise DisclosureProjectionError from None


def _require_binding_matches_plan(
    *, plan: ExecutionPlan, binding: DisclosureProjectionBinding
) -> None:
    if (
        binding.capability_id != plan.capability_id
        or binding.capability_version != plan.capability_version
    ):
        raise DisclosureProjectionError


def _project_confirmed_slots(
    *,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    binding: DisclosureProjectionBinding,
) -> tuple[ConfirmedSlot, ...]:
    projector = binding.confirmed_slot_projector
    if projector is None:
        if binding.allowed_clarification_fields:
            raise DisclosureProjectionError
        return ()
    try:
        projected = projector(plan=plan, target=target)
    except Exception:
        raise DisclosureProjectionError from None
    return validate_confirmed_snapshot(
        projected, allowed_fields=binding.allowed_clarification_fields
    )


def _plan_disposition(
    *,
    has_side_effect: bool,
    read_classes: tuple[ReadClass, ...],
) -> ExecutionDisclosureDisposition:
    if has_side_effect:
        return ExecutionDisclosureDisposition.SIDE_EFFECT
    if ReadClass.RESTRICTED in read_classes:
        return ExecutionDisclosureDisposition.RESTRICTED_READ
    return ExecutionDisclosureDisposition.BOUNDED_READ


def _slot_changes(
    *,
    parent: tuple[ConfirmedSlot, ...],
    current: tuple[ConfirmedSlot, ...],
) -> tuple[ExecutionDisclosureSlotChange, ...]:
    parent_by_field = {slot.field: slot.value for slot in parent}
    current_by_field = {slot.field: slot.value for slot in current}
    changes: list[ExecutionDisclosureSlotChange] = []
    for field in sorted(parent_by_field | current_by_field, key=lambda item: item.value):
        previous = parent_by_field.get(field)
        current_value = current_by_field.get(field)
        if previous != current_value:
            changes.append(
                ExecutionDisclosureSlotChange(
                    field=field,
                    previous=previous,
                    current=current_value,
                )
            )
    return tuple(changes)

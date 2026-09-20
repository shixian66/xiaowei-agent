"""Execution disclosure contracts.

The disclosure is a persisted/auditable readiness fact before admission and
gateway. It does not mean a channel delivered the text, a user confirmed it, or
that any fake/recording adapter has connected to a real target.
"""

from typing import Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import AlwaysTrue, Contract, StrictStr
from xiaowei_agent.contracts.clarification import ConfirmedSlot, ConfirmedValue
from xiaowei_agent.contracts.enums import (
    ClarificationField,
    EffectClass,
    ExecutionDisclosureDisposition,
    ReadClass,
)


class ExecutionDisclosureStep(Contract):
    step_id: StrictStr
    operation: StrictStr
    effect_class: EffectClass
    read_class: ReadClass | None
    side_effect: bool


class ExecutionDisclosureSlotChange(Contract):
    field: ClarificationField
    previous: ConfirmedValue | None
    current: ConfirmedValue | None

    @model_validator(mode="after")
    def _must_change(self) -> Self:
        if self.previous == self.current:
            raise ValueError("slot change must differ")
        return self


class ExecutionDisclosure(Contract):
    capability_id: StrictStr
    capability_version: StrictStr
    environment_id: StrictStr
    provider: StrictStr
    resource_kind: StrictStr
    resource_ids: tuple[StrictStr, ...] = Field(min_length=1)
    pure_read_only: bool
    plan_disposition: ExecutionDisclosureDisposition
    read_classes: tuple[ReadClass, ...]
    has_side_effect: bool
    steps: tuple[ExecutionDisclosureStep, ...] = Field(min_length=1)
    confirmed_slots: tuple[ConfirmedSlot, ...] = ()
    parent_changes: tuple[ExecutionDisclosureSlotChange, ...] = ()
    external_target_access: AlwaysTrue = True

    @model_validator(mode="after")
    def _disposition_matches_flags(self) -> Self:
        if self.pure_read_only == self.has_side_effect:
            raise ValueError("pure_read_only and has_side_effect must be complements")
        if self.plan_disposition is ExecutionDisclosureDisposition.SIDE_EFFECT:
            if self.pure_read_only or not self.has_side_effect:
                raise ValueError("side-effect disclosure must not be pure read-only")
        elif not self.pure_read_only or self.has_side_effect:
            raise ValueError("read disclosure must be pure read-only")
        if (
            self.plan_disposition is ExecutionDisclosureDisposition.RESTRICTED_READ
            and ReadClass.RESTRICTED not in self.read_classes
        ):
            raise ValueError("restricted read disclosure must include restricted read class")
        return self

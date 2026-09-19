"""Typed capability input binding and slot verification contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from xiaowei_agent.contracts import (
    Candidate,
    CapabilityParams,
    CapabilitySnapshot,
    ClarificationContext,
    ClarificationField,
    ConfirmedSlot,
    ExecutionPlan,
    IntentDraft,
    RequestContext,
    ResolvedTarget,
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
    SlotVerificationResult,
)

if TYPE_CHECKING:
    from xiaowei_agent.application.capability_runtime import PreparedCapability

ParamsT = TypeVar("ParamsT", bound=CapabilityParams)
ParamsContraT = TypeVar("ParamsContraT", bound=CapabilityParams, contravariant=True)

__all__ = [
    "CapabilityInputBinding",
    "CapabilityInputBindingError",
    "CapabilityPlanner",
    "ConfirmedSlotProjector",
    "SlotIncomplete",
    "SlotInvalid",
    "SlotReady",
    "SlotVerificationResult",
    "SlotVerifier",
    "bind_capability_input",
    "require_exact_params_type",
]


class CapabilityInputBindingError(RuntimeError):
    """Capability input binding is internally inconsistent."""


class SlotVerifier(Protocol[ParamsT]):
    def __call__(
        self,
        *,
        candidate: Candidate,
        draft: IntentDraft,
        context: RequestContext,
        as_of: dt.datetime,
        user_text: str,
        clarification: ClarificationContext | None = None,
    ) -> SlotVerificationResult[ParamsT]: ...


class CapabilityPlanner(Protocol[ParamsContraT]):
    def __call__(
        self,
        *,
        candidate: Candidate,
        params: ParamsContraT,
        context: RequestContext,
        snapshot: CapabilitySnapshot,
    ) -> PreparedCapability: ...


class ConfirmedSlotProjector(Protocol):
    def __call__(
        self,
        *,
        plan: ExecutionPlan,
        target: ResolvedTarget,
    ) -> tuple[ConfirmedSlot, ...]: ...


@dataclass(frozen=True)
class CapabilityInputBinding(Generic[ParamsT]):
    """Binds a capability declaration to its typed input verifier and planner."""

    params_type: type[ParamsT]
    input_schema_ref: str
    allowed_clarification_fields: frozenset[ClarificationField]
    slot_verifier: SlotVerifier[ParamsT]
    planner: CapabilityPlanner[ParamsT]
    confirmed_slot_projector: ConfirmedSlotProjector | None

    def __post_init__(self) -> None:
        _validate_params_type(self.params_type, self.input_schema_ref)
        fields = frozenset(self.allowed_clarification_fields)
        if any(not isinstance(field, ClarificationField) for field in fields):
            raise CapabilityInputBindingError("invalid clarification field allowlist")
        object.__setattr__(self, "allowed_clarification_fields", fields)
        if not callable(self.slot_verifier):
            raise CapabilityInputBindingError("slot verifier must be callable")
        if not callable(self.planner):
            raise CapabilityInputBindingError("capability planner must be callable")
        if self.confirmed_slot_projector is not None and not callable(
            self.confirmed_slot_projector
        ):
            raise CapabilityInputBindingError("confirmed slot projector must be callable")
        if fields and self.confirmed_slot_projector is None:
            raise CapabilityInputBindingError(
                "clarifiable capability binding requires a confirmed slot projector"
            )


def bind_capability_input(
    *,
    params_type: type[ParamsT],
    input_schema_ref: str,
    allowed_clarification_fields: frozenset[ClarificationField],
    slot_verifier: SlotVerifier[ParamsT],
    planner: CapabilityPlanner[ParamsT],
    confirmed_slot_projector: ConfirmedSlotProjector | None,
) -> CapabilityInputBinding[ParamsT]:
    """Construct a typed capability input binding with runtime invariants."""
    return CapabilityInputBinding(
        params_type=params_type,
        input_schema_ref=input_schema_ref,
        allowed_clarification_fields=allowed_clarification_fields,
        slot_verifier=slot_verifier,
        planner=planner,
        confirmed_slot_projector=confirmed_slot_projector,
    )


def require_exact_params_type(
    binding: CapabilityInputBinding[ParamsT], result: SlotReady[CapabilityParams]
) -> ParamsT:
    """Return params only when verifier output exactly matches the binding type."""
    if type(result.params) is not binding.params_type:
        raise CapabilityInputBindingError(
            "slot verifier returned an unexpected params type"
        )
    return result.params


def _validate_params_type(
    params_type: type[CapabilityParams], input_schema_ref: str
) -> None:
    if not isinstance(input_schema_ref, str) or input_schema_ref.strip() != input_schema_ref:
        raise CapabilityInputBindingError("input schema ref must be non-empty and unpadded")
    if not input_schema_ref:
        raise CapabilityInputBindingError("input schema ref must be non-empty")
    if not isinstance(params_type, type) or not issubclass(
        params_type, CapabilityParams
    ):
        raise CapabilityInputBindingError("params type must extend CapabilityParams")
    if params_type is CapabilityParams:
        raise CapabilityInputBindingError("params type must be capability-specific")
    if params_type.INPUT_SCHEMA_REF != input_schema_ref:
        raise CapabilityInputBindingError("params input schema ref differs")

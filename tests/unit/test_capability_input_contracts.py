"""Capability input binding is the only typed slot upgrade boundary."""

from typing import ClassVar

import pytest

from xiaowei_agent.application.capability_input import (
    CapabilityInputBindingError,
    SlotReady,
    bind_capability_input,
    require_exact_params_type,
)
from xiaowei_agent.contracts import CapabilityParams, ClarificationField


class DemoParams(CapabilityParams):
    INPUT_SCHEMA_REF: ClassVar[str] = "input.demo.v1"


class OtherParams(CapabilityParams):
    INPUT_SCHEMA_REF: ClassVar[str] = "input.other.v1"


class DerivedDemoParams(DemoParams):
    pass


def _verifier(**_: object) -> SlotReady[DemoParams]:
    return SlotReady(params=DemoParams(), confirmed_slots=())


def _planner(**_: object) -> object:
    raise AssertionError("planner is not called by binding construction")


def _projector(**_: object) -> tuple[object, ...]:
    return ()


def test_binding_rejects_non_capability_params_type() -> None:
    with pytest.raises(CapabilityInputBindingError):
        bind_capability_input(
            params_type=object,
            input_schema_ref="input.demo.v1",
            allowed_clarification_fields=frozenset(),
            slot_verifier=_verifier,
            planner=_planner,
            confirmed_slot_projector=None,
        )


def test_binding_rejects_the_abstract_params_base() -> None:
    with pytest.raises(CapabilityInputBindingError):
        bind_capability_input(
            params_type=CapabilityParams,
            input_schema_ref="input.demo.v1",
            allowed_clarification_fields=frozenset(),
            slot_verifier=_verifier,
            planner=_planner,
            confirmed_slot_projector=None,
        )


def test_binding_rejects_schema_drift_between_ref_and_params_type() -> None:
    with pytest.raises(CapabilityInputBindingError):
        bind_capability_input(
            params_type=OtherParams,
            input_schema_ref="input.demo.v1",
            allowed_clarification_fields=frozenset(),
            slot_verifier=_verifier,
            planner=_planner,
            confirmed_slot_projector=None,
        )


def test_clarifiable_binding_requires_confirmed_slot_projector() -> None:
    with pytest.raises(CapabilityInputBindingError):
        bind_capability_input(
            params_type=DemoParams,
            input_schema_ref="input.demo.v1",
            allowed_clarification_fields=frozenset({ClarificationField.DATABASE}),
            slot_verifier=_verifier,
            planner=_planner,
            confirmed_slot_projector=None,
        )


def test_binding_rejects_non_clarification_field_allowlist_entries() -> None:
    with pytest.raises(CapabilityInputBindingError):
        bind_capability_input(
            params_type=DemoParams,
            input_schema_ref="input.demo.v1",
            allowed_clarification_fields=frozenset({"database"}),  # type: ignore[arg-type]
            slot_verifier=_verifier,
            planner=_planner,
            confirmed_slot_projector=_projector,
        )


def test_binding_accepts_input_ref_independent_from_operation_argument_refs() -> None:
    binding = bind_capability_input(
        params_type=DemoParams,
        input_schema_ref="input.demo.v1",
        allowed_clarification_fields=frozenset(),
        slot_verifier=_verifier,
        planner=_planner,
        confirmed_slot_projector=None,
    )
    assert binding.input_schema_ref == "input.demo.v1"


def test_ready_params_must_match_the_binding_exact_type_not_subclass() -> None:
    binding = bind_capability_input(
        params_type=DemoParams,
        input_schema_ref="input.demo.v1",
        allowed_clarification_fields=frozenset(),
        slot_verifier=_verifier,
        planner=_planner,
        confirmed_slot_projector=None,
    )
    ready = SlotReady(params=DerivedDemoParams(), confirmed_slots=())

    with pytest.raises(CapabilityInputBindingError):
        require_exact_params_type(binding, ready)


def test_ready_params_exact_type_is_returned() -> None:
    binding = bind_capability_input(
        params_type=DemoParams,
        input_schema_ref="input.demo.v1",
        allowed_clarification_fields=frozenset(),
        slot_verifier=_verifier,
        planner=_planner,
        confirmed_slot_projector=None,
    )
    ready = SlotReady(params=DemoParams(), confirmed_slots=())

    assert require_exact_params_type(binding, ready) == ready.params

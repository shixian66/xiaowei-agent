"""Disclosure projection must fail closed before admission/gateway."""

import pytest
from tests.unit.test_execution_disclosure import _binding, _plan, _target

from xiaowei_agent.capabilities.specs import CAPABILITY_ID, CAPABILITY_VERSION
from xiaowei_agent.contracts import ClarificationField, ConfirmedSlot
from xiaowei_agent.planning.disclosure import (
    DisclosureProjectionBinding,
    DisclosureProjectionError,
    project_execution_disclosure,
)
from xiaowei_agent.planning.slot_verification import confirmed_text_value

pytestmark = pytest.mark.security


def test_disclosure_rejects_binding_key_mismatch_without_leaking_value() -> None:
    secret_shaped = "token=" + "abc123def4567890"
    binding = DisclosureProjectionBinding(
        capability_id=secret_shaped,
        capability_version=CAPABILITY_VERSION,
        allowed_clarification_fields=frozenset(),
        confirmed_slot_projector=None,
    )

    with pytest.raises(DisclosureProjectionError) as exc:
        project_execution_disclosure(plan=_plan(), target=_target(), binding=binding)

    assert str(exc.value) == "disclosure.projection_failed"
    assert secret_shaped not in str(exc.value)


def test_disclosure_rejects_missing_projector_for_clarifiable_binding() -> None:
    binding = DisclosureProjectionBinding(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        allowed_clarification_fields=frozenset({ClarificationField.DATABASE}),
        confirmed_slot_projector=None,
    )

    with pytest.raises(DisclosureProjectionError):
        project_execution_disclosure(plan=_plan(), target=_target(), binding=binding)


def test_disclosure_rejects_projected_slots_outside_binding_allowlist() -> None:
    def _bad_projector(**_: object) -> tuple[ConfirmedSlot, ...]:
        return (
            ConfirmedSlot(
                field=ClarificationField.DATABASE,
                value=confirmed_text_value("analytics"),
            ),
        )

    binding = DisclosureProjectionBinding(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        allowed_clarification_fields=frozenset({ClarificationField.TIME_RANGE}),
        confirmed_slot_projector=_bad_projector,
    )

    with pytest.raises(DisclosureProjectionError):
        project_execution_disclosure(plan=_plan(), target=_target(), binding=binding)


def test_projector_error_is_closed_and_does_not_leak_original_message() -> None:
    secret_shaped = "hunter" + "2-plain"

    def _raising_projector(**_: object) -> tuple[ConfirmedSlot, ...]:
        raise RuntimeError(secret_shaped)

    binding = DisclosureProjectionBinding(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        allowed_clarification_fields=frozenset({ClarificationField.DATABASE}),
        confirmed_slot_projector=_raising_projector,
    )

    with pytest.raises(DisclosureProjectionError) as exc:
        project_execution_disclosure(plan=_plan(), target=_target(), binding=binding)

    assert str(exc.value) == "disclosure.projection_failed"
    assert secret_shaped not in str(exc.value)


def test_parent_snapshot_is_revalidated_against_binding_allowlist() -> None:
    parent = (
        ConfirmedSlot(
            field=ClarificationField.ASSET_ID,
            value=confirmed_text_value("asset-1"),
        ),
    )

    with pytest.raises(DisclosureProjectionError):
        project_execution_disclosure(
            plan=_plan(),
            target=_target(),
            binding=_binding(),
            parent_confirmed_slots=parent,
        )

"""Shared helpers for deterministic confirmed slot snapshots."""

import datetime as dt

import pytest

from xiaowei_agent.contracts import ClarificationField
from xiaowei_agent.planning.slot_verification import (
    SlotCandidate,
    SlotSource,
    SlotVerificationError,
    canonical_time_range_value,
    confirmed_text_value,
    merge_confirmed_slots,
    require_confirmed_projection,
    validate_confirmed_snapshot,
)


def _slot(field: ClarificationField, text: str):
    return SlotCandidate(
        field=field,
        value=confirmed_text_value(text),
        source=SlotSource.CURRENT_TEXT,
    )


def _time_slot(start: dt.datetime, end: dt.datetime) -> SlotCandidate:
    return SlotCandidate(
        field=ClarificationField.TIME_RANGE,
        value=canonical_time_range_value(start_utc=start, end_utc=end),
        source=SlotSource.CURRENT_TEXT,
    )


def test_current_text_candidate_becomes_sorted_confirmed_snapshot() -> None:
    merged = merge_confirmed_slots(
        parent_snapshot=(),
        current_candidates=(
            _slot(ClarificationField.USER_NAME, "alice"),
            _slot(ClarificationField.DATABASE, "analytics"),
        ),
        allowed_fields=frozenset(
            {ClarificationField.DATABASE, ClarificationField.USER_NAME}
        ),
    )

    assert tuple(slot.field for slot in merged) == (
        ClarificationField.DATABASE,
        ClarificationField.USER_NAME,
    )


def test_parent_snapshot_is_inherited_without_recomputing_time() -> None:
    parent_start = dt.datetime(2026, 9, 19, 2, 30, tzinfo=dt.UTC)
    parent_end = dt.datetime(2026, 9, 19, 3, 0, tzinfo=dt.UTC)
    parent = merge_confirmed_slots(
        parent_snapshot=(),
        current_candidates=(_time_slot(parent_start, parent_end),),
        allowed_fields=frozenset({ClarificationField.TIME_RANGE}),
    )

    child_as_of = dt.datetime(2026, 9, 19, 4, 0, tzinfo=dt.UTC)
    _ = child_as_of
    inherited = merge_confirmed_slots(
        parent_snapshot=parent,
        current_candidates=(),
        allowed_fields=frozenset({ClarificationField.TIME_RANGE}),
    )

    assert inherited == parent


def test_current_text_replaces_parent_value_for_the_same_field() -> None:
    parent = merge_confirmed_slots(
        parent_snapshot=(),
        current_candidates=(_slot(ClarificationField.DATABASE, "analytics"),),
        allowed_fields=frozenset({ClarificationField.DATABASE}),
    )

    merged = merge_confirmed_slots(
        parent_snapshot=parent,
        current_candidates=(_slot(ClarificationField.DATABASE, "warehouse"),),
        allowed_fields=frozenset({ClarificationField.DATABASE}),
    )

    assert merged[0].value.text == "warehouse"


def test_multiple_current_values_for_one_field_are_ambiguous() -> None:
    with pytest.raises(SlotVerificationError):
        merge_confirmed_slots(
            parent_snapshot=(),
            current_candidates=(
                _slot(ClarificationField.DATABASE, "analytics"),
                _slot(ClarificationField.DATABASE, "warehouse"),
            ),
            allowed_fields=frozenset({ClarificationField.DATABASE}),
        )


def test_non_allowed_field_is_rejected() -> None:
    with pytest.raises(SlotVerificationError):
        merge_confirmed_slots(
            parent_snapshot=(),
            current_candidates=(_slot(ClarificationField.USER_NAME, "alice"),),
            allowed_fields=frozenset({ClarificationField.DATABASE}),
        )


def test_non_utc_input_is_canonicalized_to_utc() -> None:
    start = dt.datetime(
        2026, 9, 19, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))
    )
    end = dt.datetime(
        2026, 9, 19, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))
    )

    value = canonical_time_range_value(start_utc=start, end_utc=end)

    assert value.start_utc == dt.datetime(2026, 9, 19, 2, 0, tzinfo=dt.UTC)
    assert value.end_utc == dt.datetime(2026, 9, 19, 2, 30, tzinfo=dt.UTC)


def test_unsorted_snapshot_is_rejected() -> None:
    snapshot = (
        _slot(ClarificationField.USER_NAME, "alice").to_confirmed_slot(),
        _slot(ClarificationField.DATABASE, "analytics").to_confirmed_slot(),
    )

    with pytest.raises(SlotVerificationError):
        validate_confirmed_snapshot(
            snapshot,
            allowed_fields=frozenset(
                {ClarificationField.DATABASE, ClarificationField.USER_NAME}
            ),
        )


def test_final_projection_must_cover_confirmed_slots() -> None:
    confirmed = merge_confirmed_slots(
        parent_snapshot=(),
        current_candidates=(_slot(ClarificationField.DATABASE, "analytics"),),
        allowed_fields=frozenset({ClarificationField.DATABASE}),
    )
    projected = merge_confirmed_slots(
        parent_snapshot=(),
        current_candidates=(_slot(ClarificationField.USER_NAME, "alice"),),
        allowed_fields=frozenset({ClarificationField.USER_NAME}),
    )

    with pytest.raises(SlotVerificationError):
        require_confirmed_projection(confirmed=confirmed, projected=projected)


def test_final_projection_may_include_deterministic_defaults() -> None:
    confirmed = merge_confirmed_slots(
        parent_snapshot=(),
        current_candidates=(_slot(ClarificationField.DATABASE, "analytics"),),
        allowed_fields=frozenset(
            {ClarificationField.DATABASE, ClarificationField.USER_NAME}
        ),
    )
    projected = merge_confirmed_slots(
        parent_snapshot=confirmed,
        current_candidates=(_slot(ClarificationField.USER_NAME, "system_default"),),
        allowed_fields=frozenset(
            {ClarificationField.DATABASE, ClarificationField.USER_NAME}
        ),
    )

    assert require_confirmed_projection(confirmed=confirmed, projected=projected) == projected

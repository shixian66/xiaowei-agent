"""Pure helpers for deterministic confirmed slot verification."""

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from xiaowei_agent.contracts import (
    ClarificationField,
    ConfirmedSlot,
    ConfirmedTextValue,
    ConfirmedTimeRangeValue,
    ConfirmedValue,
)
from xiaowei_agent.contracts.clarification import TimezoneId


class SlotVerificationError(ValueError):
    """A value cannot safely become or remain a confirmed slot."""


class SlotSource(StrEnum):
    CURRENT_TEXT = "current_text"
    MODEL = "model"
    EXTERNAL_CONTENT = "external_content"


@dataclass(frozen=True)
class SlotCandidate:
    field: ClarificationField
    value: ConfirmedValue
    source: SlotSource

    def to_confirmed_slot(self) -> ConfirmedSlot:
        return ConfirmedSlot(field=self.field, value=self.value)


def confirmed_text_value(text: str) -> ConfirmedTextValue:
    """Build a safe confirmed text value or raise without leaking the raw value."""
    try:
        return ConfirmedTextValue(kind="text", text=text)
    except ValidationError as exc:
        raise ValueError("text cannot be confirmed") from exc


def canonical_time_range_value(
    *, start_utc: dt.datetime, end_utc: dt.datetime, timezone_id: TimezoneId = "UTC"
) -> ConfirmedTimeRangeValue:
    """Return a canonical UTC half-open time range."""
    if start_utc.tzinfo is None or start_utc.tzinfo.utcoffset(start_utc) is None:
        raise SlotVerificationError("time range endpoints must be timezone-aware")
    if end_utc.tzinfo is None or end_utc.tzinfo.utcoffset(end_utc) is None:
        raise SlotVerificationError("time range endpoints must be timezone-aware")
    try:
        return ConfirmedTimeRangeValue(
            kind="time_range",
            start_utc=start_utc.astimezone(dt.UTC),
            end_utc=end_utc.astimezone(dt.UTC),
            timezone_id=timezone_id,
        )
    except ValidationError as exc:
        raise SlotVerificationError("time range cannot be confirmed") from exc


def validate_confirmed_snapshot(
    slots: tuple[ConfirmedSlot, ...],
    *,
    allowed_fields: frozenset[ClarificationField],
) -> tuple[ConfirmedSlot, ...]:
    """Validate canonical field order, uniqueness and binding allowlist."""
    keys = tuple(slot.field.value for slot in slots)
    if keys != tuple(sorted(keys)):
        raise SlotVerificationError("confirmed slots must be sorted by field")
    if len(set(keys)) != len(keys):
        raise SlotVerificationError("confirmed slot fields must be unique")
    if any(slot.field not in allowed_fields for slot in slots):
        raise SlotVerificationError("confirmed slot field is not allowed")
    return slots


def merge_confirmed_slots(
    *,
    parent_snapshot: tuple[ConfirmedSlot, ...],
    current_candidates: tuple[SlotCandidate, ...],
    allowed_fields: frozenset[ClarificationField],
) -> tuple[ConfirmedSlot, ...]:
    """Merge parent confirmed slots with current explicitly confirmed text."""
    merged = {
        slot.field: slot
        for slot in validate_confirmed_snapshot(
            parent_snapshot, allowed_fields=allowed_fields
        )
    }
    current_by_field: dict[ClarificationField, ConfirmedSlot] = {}
    for candidate in current_candidates:
        if candidate.source is not SlotSource.CURRENT_TEXT:
            raise SlotVerificationError("untrusted slot source cannot be confirmed")
        if candidate.field not in allowed_fields:
            raise SlotVerificationError("confirmed slot field is not allowed")
        slot = candidate.to_confirmed_slot()
        existing = current_by_field.get(candidate.field)
        if existing is not None and existing != slot:
            raise SlotVerificationError("multiple current values for one field")
        current_by_field[candidate.field] = slot
    merged.update(current_by_field)
    return validate_confirmed_snapshot(
        tuple(sorted(merged.values(), key=lambda slot: slot.field.value)),
        allowed_fields=allowed_fields,
    )


def require_confirmed_projection(
    *,
    confirmed: tuple[ConfirmedSlot, ...],
    projected: tuple[ConfirmedSlot, ...],
) -> tuple[ConfirmedSlot, ...]:
    """Require every confirmed slot to be present with the same value in projection."""
    all_fields = frozenset(ClarificationField)
    confirmed = validate_confirmed_snapshot(confirmed, allowed_fields=all_fields)
    projected = validate_confirmed_snapshot(projected, allowed_fields=all_fields)
    projected_by_field = {slot.field: slot for slot in projected}
    for slot in confirmed:
        if projected_by_field.get(slot.field) != slot:
            raise SlotVerificationError("projected slots do not cover confirmed slots")
    return projected

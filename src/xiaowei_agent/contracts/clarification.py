"""I1 终态澄清契约。"""

import datetime as dt
import re
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    NonEmptyText,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.enums import (
    ClarificationField,
    ClarificationReasonCode,
    InteractionKind,
)
from xiaowei_agent.redaction import scrub_text

_CLARIFICATION_TASK_ID_MAX_LENGTH: Final[int] = 200
_CLARIFICATION_TASK_ID_PATTERN: Final[str] = (
    rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{_CLARIFICATION_TASK_ID_MAX_LENGTH - 1}}}"
)
_CLARIFICATION_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(
    _CLARIFICATION_TASK_ID_PATTERN
)


def _clarification_task_id(value: str) -> str:
    if _CLARIFICATION_TASK_ID_RE.fullmatch(value) is None:
        raise ValueError("task_id has an invalid shape")
    return value


ClarificationTaskId = Annotated[StrictStr, AfterValidator(_clarification_task_id)]


class ConfirmedTextValue(Contract):
    kind: Literal["text"]
    text: StrictStr = Field(max_length=512)

    @model_validator(mode="after")
    def _text_is_safe_and_bounded(self) -> Self:
        if len(self.text.encode("utf-8")) > 2048:
            raise ValueError("confirmed text exceeds the UTF-8 byte limit")
        if scrub_text(self.text) != self.text:
            raise ValueError("confirmed text must not require redaction")
        return self


class ConfirmedTimeRangeValue(Contract):
    kind: Literal["time_range"]
    start_utc: AwareDatetime
    end_utc: AwareDatetime
    timezone_id: StrictStr

    @model_validator(mode="after")
    def _range_is_canonical_utc_and_half_open(self) -> Self:
        if self.start_utc.tzinfo is not dt.UTC or self.end_utc.tzinfo is not dt.UTC:
            raise ValueError("time range endpoints must be canonical UTC")
        if self.start_utc >= self.end_utc:
            raise ValueError("time range must be a non-empty half-open interval")
        return self


ConfirmedValue = Annotated[
    ConfirmedTextValue | ConfirmedTimeRangeValue,
    Field(discriminator="kind"),
]


class ConfirmedSlot(Contract):
    field: ClarificationField
    value: ConfirmedValue


class RouteSubject(Contract):
    kind: Literal["route"]
    proposed_kind: InteractionKind
    confirmed_slots: tuple[ConfirmedSlot, ...] = ()

    @model_validator(mode="after")
    def _route_subject_has_no_slots(self) -> Self:
        if self.confirmed_slots:
            raise ValueError("route subject cannot carry confirmed slots")
        return self


class CapabilitySubject(Contract):
    kind: Literal["capability"]
    capability_id: StrictStr
    capability_version: StrictStr
    operation: StrictStr
    input_schema_ref: StrictStr
    confirmed_slots: tuple[ConfirmedSlot, ...] = ()


ClarificationSubject = Annotated[
    RouteSubject | CapabilitySubject,
    Field(discriminator="kind"),
]


def _validate_slot_snapshot(slots: tuple[ConfirmedSlot, ...]) -> None:
    keys = tuple(slot.field.value for slot in slots)
    if keys != tuple(sorted(keys)):
        raise ValueError("confirmed slots must be sorted by field")
    if len(set(keys)) != len(keys):
        raise ValueError("confirmed slot fields must be unique")


class ClarificationRecord(Contract):
    record_version: Literal[1] = 1
    task_id: ClarificationTaskId
    subject: ClarificationSubject
    reason_code: ClarificationReasonCode
    missing_fields: tuple[ClarificationField, ...]
    confirmed_slots: tuple[ConfirmedSlot, ...]
    created_at: AwareDatetime
    fencing_token: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _snapshot_is_canonical(self) -> Self:
        _validate_slot_snapshot(self.confirmed_slots)
        if tuple(self.subject.confirmed_slots) != self.confirmed_slots:
            raise ValueError("subject slots must match the record snapshot")
        return self


class ClarificationPayload(Contract):
    reason_code: ClarificationReasonCode
    missing_fields: tuple[ClarificationField, ...]
    confirmed_slots: tuple[ConfirmedSlot, ...] = ()
    prompt: NonEmptyText

    @model_validator(mode="after")
    def _payload_snapshot_is_canonical(self) -> Self:
        _validate_slot_snapshot(self.confirmed_slots)
        return self

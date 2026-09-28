"""I1 终态澄清契约。"""

import datetime as dt
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    NonEmptyText,
    Sha256Hex,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.enums import (
    ClarificationField,
    ClarificationReasonCode,
    InteractionKind,
)
from xiaowei_agent.contracts.ids import TaskId
from xiaowei_agent.contracts.resource_config import BoundedText, ResourceId
from xiaowei_agent.redaction import scrub_text

TimezoneId = Literal["UTC", "Asia/Shanghai"]


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
    timezone_id: TimezoneId

    @model_validator(mode="after")
    def _range_is_canonical_utc_and_half_open(self) -> Self:
        if (
            self.start_utc.utcoffset() != dt.timedelta(0)
            or self.end_utc.utcoffset() != dt.timedelta(0)
        ):
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


class TargetOption(Contract):
    """目标选择追问的一个选项：资源 id 与管理员配置的展示名。"""

    resource_id: ResourceId
    display_name: BoundedText


class TargetSelection(Contract):
    """F1 目标选择追问要保存的事实：SQL 引用与当时列出的选项（设计 §5.4）。

    不含 SQL 原文；选项至少两个，resource id 与展示名各自唯一（回答按展示名精确匹配）。
    """

    sql_ref: StrictStr = Field(min_length=1)
    sql_hash: Sha256Hex
    options: tuple[TargetOption, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _options_are_distinguishable(self) -> Self:
        ids = [option.resource_id for option in self.options]
        names = [option.display_name for option in self.options]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("target options must be unique")
        return self


class CapabilitySubject(Contract):
    kind: Literal["capability"]
    capability_id: StrictStr
    capability_version: StrictStr
    operation: StrictStr
    input_schema_ref: StrictStr
    confirmed_slots: tuple[ConfirmedSlot, ...] = ()
    # 为空时不序列化：既有澄清记录与交互摘要的字节保持不变。
    target_selection: TargetSelection | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


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
    task_id: TaskId
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
        require_target_selection_matches_reason(self.subject, self.reason_code)
        return self


def require_target_selection_matches_reason(
    subject: "ClarificationSubject", reason_code: ClarificationReasonCode
) -> None:
    """目标选择事实只属于目标选择追问，且该追问必须带着它。"""
    selecting = reason_code is ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED
    recorded = isinstance(subject, CapabilitySubject) and subject.target_selection is not None
    if selecting != recorded:
        raise ValueError("target selection must match the clarification reason")


class ClarificationContext(Contract):
    subject: ClarificationSubject
    confirmed_slots: tuple[ConfirmedSlot, ...]
    missing_fields: tuple[ClarificationField, ...] = ()

    @model_validator(mode="after")
    def _snapshot_is_canonical(self) -> Self:
        _validate_slot_snapshot(self.confirmed_slots)
        if tuple(self.subject.confirmed_slots) != self.confirmed_slots:
            raise ValueError("subject slots must match the context snapshot")
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

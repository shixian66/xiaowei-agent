"""I1 终态澄清契约：只保存可信短槽位与闭集原因。"""

import datetime as dt

import pytest
from pydantic import TypeAdapter, ValidationError

from xiaowei_agent.contracts import (
    CapabilitySubject,
    ClarificationContext,
    ClarificationField,
    ClarificationPayload,
    ClarificationReasonCode,
    ClarificationRecord,
    ConfirmedSlot,
    ConfirmedTextValue,
    ConfirmedTimeRangeValue,
    InteractionKind,
    RouteSubject,
    TaskId,
)
from xiaowei_agent.persistence.rows import dump_contract, load_contract

_AT = dt.datetime(2026, 9, 15, 7, 30, tzinfo=dt.UTC)


def _text_slot(field: ClarificationField, text: str = "sales") -> ConfirmedSlot:
    return ConfirmedSlot(field=field, value=ConfirmedTextValue(kind="text", text=text))


def _time_slot() -> ConfirmedSlot:
    return ConfirmedSlot(
        field=ClarificationField.TIME_RANGE,
        value=ConfirmedTimeRangeValue(
            kind="time_range",
            start_utc=dt.datetime(2026, 9, 15, 6, 0, tzinfo=dt.UTC),
            end_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
            timezone_id="Asia/Shanghai",
        ),
    )


def test_route_subject_cannot_smuggle_capability_slots() -> None:
    subject = RouteSubject(kind="route", proposed_kind=InteractionKind.UNKNOWN)

    assert subject.confirmed_slots == ()
    with pytest.raises(ValidationError):
        RouteSubject(
            kind="route",
            proposed_kind=InteractionKind.UNKNOWN,
            capability_id="starrocks.slow_query.diagnose",
        )


def test_capability_subject_requires_exact_binding_identity() -> None:
    subject = CapabilitySubject(
        kind="capability",
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        operation="diagnose",
        input_schema_ref="input.starrocks.slow_query.v1",
        confirmed_slots=(_text_slot(ClarificationField.DATABASE),),
    )

    assert subject.confirmed_slots[0].field is ClarificationField.DATABASE
    with pytest.raises(ValidationError):
        CapabilitySubject(
            kind="capability",
            capability_id="starrocks.slow_query.diagnose",
            capability_version="1.0.0",
            operation="diagnose",
            confirmed_slots=(),
        )


def test_confirmed_text_value_rejects_secret_shaped_or_oversized_values() -> None:
    with pytest.raises(ValidationError):
        ConfirmedTextValue(kind="text", text="token=" + "fake-value")
    with pytest.raises(ValidationError):
        ConfirmedTextValue(kind="text", text="x" * 513)
    with pytest.raises(ValidationError):
        ConfirmedTextValue(kind="text", text="\U0001f600" * 513)


def test_confirmed_time_range_requires_canonical_utc_half_open_interval() -> None:
    value = ConfirmedTimeRangeValue(
        kind="time_range",
        start_utc=dt.datetime(2026, 9, 15, 6, 0, tzinfo=dt.UTC),
        end_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
        timezone_id="Asia/Shanghai",
    )

    assert value.start_utc.utcoffset() == dt.timedelta(0)
    with pytest.raises(ValidationError):
        ConfirmedTimeRangeValue(
            kind="time_range",
            start_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
            end_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
            timezone_id="Asia/Shanghai",
        )
    with pytest.raises(ValidationError):
        ConfirmedTimeRangeValue(
            kind="time_range",
            start_utc=dt.datetime(
                2026, 9, 15, 6, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))
            ),
            end_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
            timezone_id="Asia/Shanghai",
        )


def test_confirmed_time_range_round_trips_through_json_contract_loader() -> None:
    value = ConfirmedTimeRangeValue(
        kind="time_range",
        start_utc=dt.datetime(2026, 9, 15, 6, 0, tzinfo=dt.UTC),
        end_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
        timezone_id="UTC",
    )

    assert load_contract(ConfirmedTimeRangeValue, dump_contract(value)) == value


def test_clarification_snapshots_with_time_range_round_trip_through_json() -> None:
    slot = _time_slot()
    subject = CapabilitySubject(
        kind="capability",
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        operation="diagnose",
        input_schema_ref="input.starrocks.slow_query.v1",
        confirmed_slots=(slot,),
    )
    record = ClarificationRecord(
        task_id="task-time",
        subject=subject,
        reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
        missing_fields=(ClarificationField.DATABASE,),
        confirmed_slots=subject.confirmed_slots,
        created_at=_AT,
        fencing_token=1,
    )
    context = ClarificationContext(
        subject=subject,
        confirmed_slots=subject.confirmed_slots,
    )
    payload = ClarificationPayload(
        reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
        missing_fields=(ClarificationField.DATABASE,),
        confirmed_slots=subject.confirmed_slots,
        prompt="请补充数据库名。",
    )

    assert load_contract(ClarificationRecord, dump_contract(record)) == record
    assert load_contract(ClarificationContext, dump_contract(context)) == context
    assert load_contract(ClarificationPayload, dump_contract(payload)) == payload


@pytest.mark.parametrize("timezone_id", ["Mars/Phobos", "UTC ", "x" * 4096])
def test_confirmed_time_range_rejects_unknown_timezone_ids(
    timezone_id: str,
) -> None:
    with pytest.raises(ValidationError):
        ConfirmedTimeRangeValue(
            kind="time_range",
            start_utc=dt.datetime(2026, 9, 15, 6, 0, tzinfo=dt.UTC),
            end_utc=dt.datetime(2026, 9, 15, 7, 0, tzinfo=dt.UTC),
            timezone_id=timezone_id,
        )


def test_clarification_record_requires_sorted_slots_and_rejects_duplicate_fields() -> None:
    subject = CapabilitySubject(
        kind="capability",
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        operation="diagnose",
        input_schema_ref="input.starrocks.slow_query.v1",
        confirmed_slots=(
            _text_slot(ClarificationField.DATABASE, "sales"),
            _text_slot(ClarificationField.USER_NAME, "app"),
        ),
    )

    record = ClarificationRecord(
        task_id="task-1",
        subject=subject,
        reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
        missing_fields=(ClarificationField.TIME_RANGE,),
        confirmed_slots=subject.confirmed_slots,
        created_at=_AT,
        fencing_token=1,
    )

    assert tuple(slot.field for slot in record.confirmed_slots) == (
        ClarificationField.DATABASE,
        ClarificationField.USER_NAME,
    )
    with pytest.raises(ValidationError):
        ClarificationRecord(
            task_id="task-unsorted",
            subject=subject,
            reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
            missing_fields=(ClarificationField.TIME_RANGE,),
            confirmed_slots=(
                _text_slot(ClarificationField.USER_NAME, "app"),
                _text_slot(ClarificationField.DATABASE, "sales"),
            ),
            created_at=_AT,
            fencing_token=1,
        )
    with pytest.raises(ValidationError):
        ClarificationRecord(
            task_id="task-2",
            subject=subject,
            reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
            missing_fields=(ClarificationField.TIME_RANGE,),
            confirmed_slots=(
                _text_slot(ClarificationField.DATABASE, "sales"),
                _text_slot(ClarificationField.DATABASE, "ops"),
            ),
            created_at=_AT,
            fencing_token=1,
        )


@pytest.mark.parametrize("bad_task_id", ["_task", "task/path", "a" * 201])
def test_clarification_record_rejects_invalid_task_id_shapes(
    bad_task_id: str,
) -> None:
    subject = RouteSubject(kind="route", proposed_kind=InteractionKind.UNKNOWN)

    with pytest.raises(ValidationError):
        ClarificationRecord(
            task_id=bad_task_id,
            subject=subject,
            reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
            missing_fields=(),
            confirmed_slots=(),
            created_at=_AT,
            fencing_token=1,
        )


def test_clarification_record_uses_shared_task_id_domain() -> None:
    task_id = "a" + ("-" * 199)
    subject = RouteSubject(kind="route", proposed_kind=InteractionKind.UNKNOWN)

    record = ClarificationRecord(
        task_id=task_id,
        subject=subject,
        reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
        missing_fields=(),
        confirmed_slots=(),
        created_at=_AT,
        fencing_token=1,
    )

    assert TypeAdapter(TaskId).validate_python(task_id, strict=True) == record.task_id


def test_clarification_payload_is_safe_and_distinct_from_render_payload() -> None:
    payload = ClarificationPayload(
        reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
        missing_fields=(ClarificationField.TIME_RANGE,),
        prompt="请补充时间范围。",
    )

    assert set(type(payload).model_fields) == {
        "reason_code",
        "missing_fields",
        "confirmed_slots",
        "prompt",
    }
    with pytest.raises(ValidationError):
        ClarificationPayload(
            reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
            missing_fields=(ClarificationField.TIME_RANGE,),
            prompt="",
            answer="must-not-be-render",
        )

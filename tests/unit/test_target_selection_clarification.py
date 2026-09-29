"""目标选择追问的澄清事实与提示（设计 §5.4）。"""

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    CapabilitySubject,
    ClarificationReasonCode,
    ClarificationRecord,
)
from xiaowei_agent.contracts.clarification import TargetOption, TargetSelection
from xiaowei_agent.persistence.clarification_records import ClarificationRecordCandidate
from xiaowei_agent.persistence.rows import dump_contract
from xiaowei_agent.rendering.generic import render_clarification_payload

_SELECTION = TargetSelection(
    sql_ref="ref-secret-token",
    sql_hash="c" * 64,
    options=(
        TargetOption(resource_id="a" * 32, display_name="Orders"),
        TargetOption(resource_id="b" * 32, display_name="指标库"),
    ),
)


def _subject(selection: TargetSelection | None) -> CapabilitySubject:
    return CapabilitySubject(
        kind="capability",
        capability_id="starrocks.readonly_query",
        capability_version="1.0.0",
        operation="execute_readonly_query",
        input_schema_ref="input.starrocks.readonly_query.v1",
        target_selection=selection,
    )


def _record(
    reason: ClarificationReasonCode, selection: TargetSelection | None
) -> ClarificationRecord:
    return ClarificationRecord(
        task_id="task-1",
        subject=_subject(selection),
        reason_code=reason,
        missing_fields=(),
        confirmed_slots=(),
        created_at=dt.datetime(2026, 9, 28, tzinfo=dt.UTC),
        fencing_token=1,
    )


def test_selection_and_reason_must_agree_on_records_and_candidates() -> None:
    selecting = ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED
    other = ClarificationReasonCode.CAPABILITY_FIELDS_MISSING
    _record(selecting, _SELECTION)
    for reason, selection in ((selecting, None), (other, _SELECTION)):
        with pytest.raises(ValidationError):
            _record(reason, selection)
        with pytest.raises(ValidationError):
            ClarificationRecordCandidate(
                subject=_subject(selection),
                reason_code=reason,
                missing_fields=(),
                confirmed_slots=(),
            )


@pytest.mark.parametrize(
    "options",
    [
        (TargetOption(resource_id="a" * 32, display_name="Orders"),),
        (
            TargetOption(resource_id="a" * 32, display_name="Orders"),
            TargetOption(resource_id="b" * 32, display_name="Orders"),
        ),
        (
            TargetOption(resource_id="a" * 32, display_name="Orders"),
            TargetOption(resource_id="a" * 32, display_name="指标库"),
        ),
    ],
)
def test_options_are_at_least_two_and_distinguishable(
    options: tuple[TargetOption, ...],
) -> None:
    with pytest.raises(ValidationError):
        TargetSelection(sql_ref="ref", sql_hash="c" * 64, options=options)


def test_subjects_without_selection_serialise_exactly_as_before() -> None:
    # 既有澄清记录与交互摘要的字节不因新增可选字段而漂移。
    dumped = dump_contract(_subject(None))
    assert "target_selection" not in dumped
    assert list(dumped) == [
        "kind",
        "capability_id",
        "capability_version",
        "operation",
        "input_schema_ref",
        "confirmed_slots",
    ]


def test_prompt_lists_names_only_without_sql_references() -> None:
    payload = render_clarification_payload(
        record=_record(
            ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED, _SELECTION
        )
    )
    assert "Orders" in payload.prompt
    assert "指标库" in payload.prompt
    rendered = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
    assert "ref-secret-token" not in rendered
    assert "c" * 64 not in rendered

"""F1 契约 DTO（ADR-018 D2、设计 §5.2/§5.6/§9.1）。"""

import datetime as dt
import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.conftest import make_envelope

from xiaowei_agent.contracts import (
    ArtifactSubmission,
    ColumnSpec,
    Completeness,
    ConversationSubmission,
    OperationSpec,
    QueryRequirement,
    ReadonlyQueryBudget,
    RequestContext,
    ResultGrantKind,
    TaskSubmission,
)
from xiaowei_agent.contracts.enums import EffectClass, ReadClass

_AS_OF = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
_SQL = b"SELECT 1"
_SQL_HASH = hashlib.sha256(_SQL).hexdigest()


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def _artifact(**updates: object) -> ArtifactSubmission:
    values: dict[str, object] = {
        "input_kind": "sql_artifact",
        "context": _context(),
        "as_of": _AS_OF,
        "sql_ref": "ref-1",
        "sql_hash": _SQL_HASH,
    }
    return ArtifactSubmission(**(values | updates))


_SUBMISSION = TypeAdapter(TaskSubmission)


def test_union_rejects_input_without_an_input_kind_tag() -> None:
    legacy = {
        "envelope": make_envelope().model_dump(mode="python"),
        "context": _context().model_dump(mode="python"),
        "as_of": _AS_OF,
    }
    with pytest.raises(ValidationError) as caught:
        _SUBMISSION.validate_python(legacy)
    assert [error["type"] for error in caught.value.errors()] == ["union_tag_not_found"]


def test_union_rejects_an_unknown_input_kind() -> None:
    with pytest.raises(ValidationError) as caught:
        _SUBMISSION.validate_python(
            {"input_kind": "raw_sql", "context": _context(), "as_of": _AS_OF}
        )
    assert [error["type"] for error in caught.value.errors()] == ["union_tag_invalid"]


def test_conversation_submission_defaults_its_tag_and_round_trips_through_the_union() -> None:
    submission = ConversationSubmission(
        envelope=make_envelope(), context=_context(), as_of=_AS_OF
    )
    assert submission.input_kind == "conversation"
    assert _SUBMISSION.validate_python(submission.model_dump()) == submission


def test_artifact_submission_round_trips_through_the_union() -> None:
    submission = _artifact()
    assert _SUBMISSION.validate_python(submission.model_dump()) == submission


def test_artifact_submission_carries_only_references() -> None:
    assert set(ArtifactSubmission.model_fields) == {
        "input_kind",
        "context",
        "as_of",
        "sql_ref",
        "sql_hash",
    }
    for extra in ("sql_text", "sql_bytes", "envelope", "target", "result_ref"):
        with pytest.raises(ValidationError):
            _artifact(**{extra: "x"})
    with pytest.raises(ValidationError):
        _artifact(sql_hash="not-a-sha256")
    with pytest.raises(ValidationError):
        _artifact(input_kind="conversation")


def test_operation_query_requirement_defaults_to_none() -> None:
    assert {member.value for member in QueryRequirement} == {
        "none",
        "template_locked",
        "confirmed_artifact",
    }
    spec = OperationSpec(
        operation="op",
        gateway="gw",
        effect_class=EffectClass.READ,
        read_class=ReadClass.RESTRICTED,
        side_effect=False,
        argument_schema_ref="schema",
    )
    assert spec.query_requirement is QueryRequirement.NONE


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [
        ("preview_max_rows", 1, 1000),
        ("preview_max_bytes", 1_048_576, 20_971_520),
        ("query_timeout_seconds", 1, 180),
    ],
)
def test_readonly_query_budget_bounds(field: str, low: int, high: int) -> None:
    base = {
        "preview_max_rows": 1000,
        "preview_max_bytes": 20_971_520,
        "query_timeout_seconds": 180,
    }
    assert getattr(ReadonlyQueryBudget(**(base | {field: low})), field) == low
    assert getattr(ReadonlyQueryBudget(**(base | {field: high})), field) == high
    for invalid in (low - 1, high + 1):
        with pytest.raises(ValidationError):
            ReadonlyQueryBudget(**(base | {field: invalid}))


def test_column_spec_and_closed_result_enums() -> None:
    column = ColumnSpec(ordinal=0, name="id", type="BIGINT")
    assert (column.ordinal, column.name, column.type) == (0, "id", "BIGINT")
    with pytest.raises(ValidationError):
        ColumnSpec(ordinal=-1, name="id", type="BIGINT")
    assert {member.value for member in Completeness} == {
        "complete",
        "truncated_rows",
        "truncated_bytes",
    }
    assert ResultGrantKind.REQUESTER_OWNER.value == "requester_owner"

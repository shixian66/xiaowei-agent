"""慢查询模型出站投影必须由 capability surface 收口。"""

import datetime as dt
import math

import pytest
from pydantic import ValidationError
from tests.fakes.admission import slow_query_plan
from tests.fakes.recordings import _row

from xiaowei_agent.application.model_advisory import (
    project_slow_query_advisory_request,
)
from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    ExternalSource,
    SlowQueryAdvisoryRequest,
    TaskOutcome,
    TaskStatus,
)


def _verdict() -> AnswerabilityVerdict:
    return AnswerabilityVerdict(
        sufficient=True,
        limitations=(),
        missing=(),
        downgrade_suggestion=False,
        needs_user_input=False,
    )


def _evidence(
    *rows: dict[str, object],
    source_kind: ExternalSource = ExternalSource.TOOL,
    evidence_ref: str = "task-1:s1",
    sampled: bool = False,
) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=evidence_ref,
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        facts=rows,
        source="starrocks-fake",
        source_kind=source_kind,
        captured_at=dt.datetime(2026, 9, 13, tzinfo=dt.UTC),
        sampled=sampled,
        limitations=(),
    )


def _outcome(
    *,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    evidence_refs: tuple[str, ...] = ("task-1:s1",),
) -> TaskOutcome:
    return TaskOutcome(
        task_id="task-1",
        status=status,
        terminal_reason=None,
        evidence_refs=evidence_refs,
        render_ref=None,
    )


def test_projector_uses_the_exact_surface_column_order() -> None:
    row = _row(0, query_time=12_000)

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(row),),
        verdict=_verdict(),
    )

    assert request is not None
    assert tuple(request.rows[0]) == SLOW_QUERY_SURFACE.allowed_columns


def test_projector_rejects_stmt_even_though_generic_dto_accepts_it() -> None:
    row = _row(0, query_time=12_000) | {"stmt": "SELECT synthetic"}
    # contracts 层刻意通用；真正的 capability 白名单必须由 application projector 强制。
    assert SlowQueryAdvisoryRequest(rows=(row,), sampled=False)

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(row),),
        verdict=_verdict(),
    )

    assert request is None


def test_field_type_partition_covers_the_surface_exactly() -> None:
    from xiaowei_agent.application import model_advisory

    assert model_advisory._TEXT_COLUMNS.isdisjoint(model_advisory._NUMBER_COLUMNS)
    assert (
        model_advisory._TEXT_COLUMNS | model_advisory._NUMBER_COLUMNS
        == frozenset(SLOW_QUERY_SURFACE.allowed_columns)
    )


@pytest.mark.parametrize("extra", ["stmt", "SQL", "clientIp", "digest"])
def test_projector_rejects_every_known_sensitive_extra_column(extra: str) -> None:
    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(_row(0, query_time=12_000) | {extra: "unsafe"}),),
        verdict=_verdict(),
    )

    assert request is None


def test_projector_rejects_a_missing_surface_column() -> None:
    row = _row(0, query_time=12_000)
    row.pop("scanRows")

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(row),),
        verdict=_verdict(),
    )

    assert request is None


@pytest.mark.parametrize("bad_index", [1, 19])
def test_projector_validates_every_selected_row_before_calling_model(
    bad_index: int,
) -> None:
    rows = [_row(index, query_time=12_000) for index in range(20)]
    rows[bad_index]["queryTime"] = "not-a-number"

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(*rows),),
        verdict=_verdict(),
    )

    assert request is None


@pytest.mark.parametrize(
    "value",
    [True, "1e3", " 1000 "],
    ids=["bool", "exponent", "padded"],
)
def test_projector_rejects_non_numeric_shapes(value: object) -> None:
    row = _row(0, query_time=12_000)
    row["queryTime"] = value

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(row),),
        verdict=_verdict(),
    )

    assert request is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_evidence_contract_rejects_non_finite_numbers_before_projection(
    value: float,
) -> None:
    row = _row(0, query_time=12_000)
    row["queryTime"] = value

    with pytest.raises(ValidationError):
        _evidence(row)


@pytest.mark.parametrize(
    "value", [dt.date(2026, 9, 13), dt.datetime(2026, 9, 13, tzinfo=dt.UTC)]
)
def test_evidence_contract_rejects_date_objects_before_projection(value: object) -> None:
    row = _row(0, query_time=12_000)
    row["timestamp"] = value

    with pytest.raises(ValidationError):
        _evidence(row)


def test_projector_rejects_invalid_utf8_text() -> None:
    row = _row(0, query_time=12_000)
    row["timestamp"] = "bad-\ud800"

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(row),),
        verdict=_verdict(),
    )

    assert request is None


@pytest.mark.parametrize(
    "status",
    [TaskStatus.FAILED, TaskStatus.REJECTED, TaskStatus.INDETERMINATE],
)
def test_projector_requires_a_successful_deterministic_outcome(
    status: TaskStatus,
) -> None:
    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(status=status),
        evidences=(_evidence(_row(0, query_time=12_000)),),
        verdict=_verdict(),
    )

    assert request is None


def test_projector_rejects_insufficient_empty_and_wrong_source_evidence() -> None:
    insufficient = _verdict().model_copy(update={"sufficient": False})
    valid = _evidence(_row(0, query_time=12_000))
    wrong_source = _evidence(
        _row(0, query_time=12_000), source_kind=ExternalSource.USER
    )

    assert project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(valid,),
        verdict=insufficient,
    ) is None
    assert project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(),),
        verdict=_verdict(),
    ) is None
    assert project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(wrong_source,),
        verdict=_verdict(),
    ) is None


def test_projector_takes_the_first_twenty_rows_and_marks_sampling() -> None:
    rows = tuple(_row(index, query_time=12_000) for index in range(21))

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(*rows),),
        verdict=_verdict(),
    )

    assert request is not None
    assert len(request.rows) == 20
    assert request.rows[0]["queryId"] == "q-0"
    assert request.rows[-1]["queryId"] == "q-19"
    assert request.sampled is True


def test_projector_rejects_an_oversized_raw_batch_before_redaction() -> None:
    oversized = _row(0, query_time=12_000)
    for column in ("queryId", "timestamp", "state", "errorCode", "db", "user"):
        oversized[column] = "token=" + "x" * (8_192 - len("token="))
    rows = tuple(dict(oversized) for _ in range(20))

    request = project_slow_query_advisory_request(
        task_id="task-1",
        plan=slow_query_plan(),
        outcome=_outcome(),
        evidences=(_evidence(*rows),),
        verdict=_verdict(),
    )

    assert request is None

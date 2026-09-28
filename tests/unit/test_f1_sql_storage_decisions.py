"""F1 SQL 存储的纯判定、digest 与行映射（ADR-018 D2/D2a/D4）。"""

import datetime as dt
import hashlib

import pytest
from pydantic import ValidationError
from tests.conftest import make_envelope

from xiaowei_agent.contracts import (
    ArtifactSubmission,
    ConversationSubmission,
    RequestContext,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.persistence.decisions import (
    SqlArtifactReadDecision,
    classify_sql_artifact_read,
    classify_sql_submit,
    extended_sql_expiry,
)
from xiaowei_agent.persistence.rows import submission_from_row, submission_to_row
from xiaowei_agent.persistence.store import (
    SQL_ARTIFACT_TTL,
    SqlArtifactExpiredError,
    SqlArtifactRecord,
    SqlArtifactUnavailableError,
    SqlArtifactUnavailableReason,
    SqlQuerySubmitCommand,
    SqlSubmitOutcome,
    TaskNotFoundError,
    idempotency_scope_digest,
    raise_for_sql_artifact_read,
    sql_request_dedup_digest,
    submission_digest,
)

_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
_SQL = b"SELECT 1"
_HASH = hashlib.sha256(_SQL).hexdigest()


def _context(**updates: str) -> RequestContext:
    values = {
        "tenant_id": "dev-local",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-2026-09-01",
    }
    return RequestContext(**(values | updates))


def _row(**updates: object) -> SqlArtifactRecord:
    values: dict[str, object] = {
        "sql_ref": "ref-1",
        "sql_hash": _HASH,
        "sql_bytes": _SQL,
        "requester": "alice",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "created_at": _NOW,
        "expires_at": _NOW + SQL_ARTIFACT_TTL,
        "purged_at": None,
    }
    return SqlArtifactRecord(**(values | updates))  # type: ignore[arg-type]


def _classify(row: SqlArtifactRecord | None, **updates: object) -> SqlArtifactReadDecision:
    values: dict[str, object] = {
        "now": _NOW,
        "requester": "alice",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "sql_hash": _HASH,
    }
    return classify_sql_artifact_read(row, **(values | updates))  # type: ignore[arg-type]


# ---- 读取分类：不存在 → 归属 → hash → 过期/已清除 ----


def test_read_classification_follows_the_fixed_order() -> None:
    assert _classify(_row()) is SqlArtifactReadDecision.AVAILABLE
    assert _classify(None) is SqlArtifactReadDecision.NOT_FOUND
    for field, value in (
        ("requester", "bob"),
        ("tenant_id", "other"),
        ("environment_id", "prod"),
    ):
        assert _classify(_row(), **{field: value}) is SqlArtifactReadDecision.SCOPE_MISMATCH
    assert _classify(_row(), sql_hash="0" * 64) is SqlArtifactReadDecision.HASH_MISMATCH
    expired = _row(expires_at=_NOW)
    assert _classify(expired) is SqlArtifactReadDecision.EXPIRED
    purged = _row(sql_bytes=None, purged_at=_NOW - dt.timedelta(minutes=1))
    assert _classify(purged) is SqlArtifactReadDecision.EXPIRED


def test_scope_and_hash_are_checked_before_expiry() -> None:
    # 他人或错绑定的 SQL 不会得到“已过期”，不泄漏存在性与状态。
    expired = _row(expires_at=_NOW - dt.timedelta(hours=1))
    assert _classify(expired, requester="bob") is SqlArtifactReadDecision.SCOPE_MISMATCH
    assert _classify(expired, sql_hash="0" * 64) is SqlArtifactReadDecision.HASH_MISMATCH


def test_read_failures_raise_the_closed_error_set() -> None:
    assert raise_for_sql_artifact_read(_row(), SqlArtifactReadDecision.AVAILABLE) == _row()
    for decision, reason in (
        (SqlArtifactReadDecision.NOT_FOUND, SqlArtifactUnavailableReason.NOT_FOUND),
        (SqlArtifactReadDecision.SCOPE_MISMATCH, SqlArtifactUnavailableReason.SCOPE_MISMATCH),
        (SqlArtifactReadDecision.HASH_MISMATCH, SqlArtifactUnavailableReason.HASH_MISMATCH),
    ):
        with pytest.raises(SqlArtifactUnavailableError) as caught:
            raise_for_sql_artifact_read(None, decision)
        assert caught.value.reason is reason
        assert caught.value.terminal_code == "sql_artifact.unavailable"
    with pytest.raises(SqlArtifactExpiredError) as expired:
        raise_for_sql_artifact_read(_row(), SqlArtifactReadDecision.EXPIRED)
    assert expired.value.terminal_code == "sql_artifact.expired"
    # 不继承 TaskNotFoundError：否则渠道提交会把它改写成 404 not_found。
    assert not issubclass(SqlArtifactUnavailableError, TaskNotFoundError)
    assert not issubclass(SqlArtifactExpiredError, TaskNotFoundError)


def test_sql_expiry_only_moves_forward() -> None:
    assert extended_sql_expiry(_NOW, now=_NOW) == _NOW + SQL_ARTIFACT_TTL
    later = _NOW + dt.timedelta(days=3)
    assert extended_sql_expiry(later, now=_NOW) == later


def test_sql_artifact_record_never_prints_sql_bytes() -> None:
    assert "SELECT" not in repr(_row())


# ---- 提交判定与幂等作用域 ----


def _existing(**updates: object) -> TaskRecord:
    values: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "actor": "alice",
        "idempotency_key": "k",
        "request_digest": "a" * 64,
        "status": TaskStatus.CREATED,
        "version": 0,
        "created_seq": 1,
        "attempt_number": 0,
        "task_failure_count": 0,
        "next_attempt_at": None,
    }
    return TaskRecord(**(values | updates))  # type: ignore[arg-type]


def _submit_decision(existing: TaskRecord | None, digest: str = "a" * 64) -> SqlSubmitOutcome:
    return classify_sql_submit(
        existing,
        tenant_id="dev-local",
        environment_id="dev",
        idempotency_key="k",
        request_digest=digest,
    )


def test_sql_submit_classification() -> None:
    assert _submit_decision(None) is SqlSubmitOutcome.CREATED
    assert _submit_decision(_existing()) is SqlSubmitOutcome.REPLAYED
    assert _submit_decision(_existing(), "b" * 64) is SqlSubmitOutcome.IDEMPOTENCY_CONFLICT
    # 作用域 digest 只是 checksum：命中后仍回查三项明文。
    for update in ({"tenant_id": "t2"}, {"environment_id": "e2"}, {"idempotency_key": "k2"}):
        assert (
            _submit_decision(_existing(**update)) is SqlSubmitOutcome.IDEMPOTENCY_CONFLICT
        )


def test_sql_and_conversation_keys_live_in_different_scopes() -> None:
    conversation = idempotency_scope_digest(
        tenant_id="dev-local", environment_id="dev", idempotency_key="k"
    )
    sql = idempotency_scope_digest(
        tenant_id="dev-local",
        environment_id="dev",
        idempotency_key="k",
        input_kind="sql_artifact",
    )
    assert conversation != sql


def test_sql_request_digest_covers_scope_hash_and_key() -> None:
    base = sql_request_dedup_digest(_context(), sql_hash=_HASH, idempotency_key="k")
    assert base == sql_request_dedup_digest(
        _context(trace_id="1" * 32), sql_hash=_HASH, idempotency_key="k"
    )
    for changed in (
        sql_request_dedup_digest(_context(actor="bob"), sql_hash=_HASH, idempotency_key="k"),
        sql_request_dedup_digest(_context(), sql_hash="0" * 64, idempotency_key="k"),
        sql_request_dedup_digest(_context(), sql_hash=_HASH, idempotency_key="k2"),
    ):
        assert changed != base


def test_artifact_submission_digest_covers_every_field() -> None:
    submission = ArtifactSubmission(
        input_kind="sql_artifact",
        context=_context(),
        as_of=_NOW,
        sql_ref="ref-1",
        sql_hash=_HASH,
    )
    base = submission_digest(submission)
    for update in (
        {"context": _context(actor="bob")},
        {"as_of": _NOW + dt.timedelta(seconds=1)},
        {"sql_ref": "ref-2"},
        {"sql_hash": "0" * 64},
    ):
        assert submission_digest(submission.model_copy(update=update)) != base


# ---- 命令 DTO ----


def test_sql_submit_command_bounds_and_hides_the_sql() -> None:
    command = SqlQuerySubmitCommand(
        context=_context(), sql_bytes=_SQL, idempotency_key="k", as_of=_NOW
    )
    assert "SELECT" not in repr(command)
    for invalid in (b"", b"x" * 65_537, b"\xff\xfe"):
        with pytest.raises(ValidationError):
            SqlQuerySubmitCommand(
                context=_context(), sql_bytes=invalid, idempotency_key="k", as_of=_NOW
            )
    assert len(
        SqlQuerySubmitCommand(
            context=_context(), sql_bytes=b"x" * 65_536, idempotency_key="k", as_of=_NOW
        ).sql_bytes
    ) == 65_536


# ---- 行映射：按 input_kind 分派，其他值或违反形状 fail-closed ----


def _conversation() -> ConversationSubmission:
    return ConversationSubmission(envelope=make_envelope(), context=_context(), as_of=_NOW)


def _artifact() -> ArtifactSubmission:
    return ArtifactSubmission(
        input_kind="sql_artifact",
        context=_context(),
        as_of=_NOW,
        sql_ref="ref-1",
        sql_hash=_HASH,
    )


@pytest.mark.parametrize("submission", [_conversation(), _artifact()])
def test_submission_rows_round_trip_by_kind(submission: object) -> None:
    row = submission_to_row(submission)  # type: ignore[arg-type]
    assert submission_from_row(row) == submission


@pytest.mark.parametrize(
    "mutation",
    [
        {"input_kind": "raw_sql"},
        {"input_kind": None},
        {"sql_ref": "ref-1"},
        {"envelope": None},
    ],
)
def test_conversation_rows_fail_closed_on_bad_shape(mutation: dict[str, object]) -> None:
    row = submission_to_row(_conversation()) | mutation
    with pytest.raises(ValueError):
        submission_from_row(row)


@pytest.mark.parametrize(
    "mutation",
    [
        {"sql_ref": None},
        {"sql_hash": None},
        {"clarification_parent_task_id": "task-parent"},
    ],
)
def test_artifact_rows_fail_closed_on_bad_shape(mutation: dict[str, object]) -> None:
    row = submission_to_row(_artifact()) | mutation
    with pytest.raises(ValueError):
        submission_from_row(row)


def test_artifact_rows_reject_an_envelope() -> None:
    row = submission_to_row(_artifact()) | {
        "envelope": submission_to_row(_conversation())["envelope"]
    }
    with pytest.raises(ValueError):
        submission_from_row(row)

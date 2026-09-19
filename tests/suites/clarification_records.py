"""ClarificationRecordStore 的内存/PostgreSQL 共享行为套件。"""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import make_submission
from tests.suites.task_store import bind

from xiaowei_agent.contracts import (
    AttemptIntent,
    ClarificationField,
    ClarificationReasonCode,
    RouteSubject,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.contracts.clarification import ClarificationRecord
from xiaowei_agent.contracts.enums import InteractionKind
from xiaowei_agent.persistence.clarification_records import (
    ClarificationRecordCandidate,
    ClarificationRecordConflictError,
    ClarificationRecordGrantError,
    ClarificationRecordStateError,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand

__all__ = ["CLARIFICATION_RECORD_CASES", "bind"]


def _candidate(**updates: object) -> ClarificationRecordCandidate:
    values: dict[str, object] = {
        "subject": RouteSubject(
            kind="route",
            proposed_kind=InteractionKind.UNKNOWN,
        ),
        "reason_code": ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
        "missing_fields": (ClarificationField.TIME_RANGE,),
        "confirmed_slots": (),
    }
    return ClarificationRecordCandidate(**(values | updates))


async def _grant(store: Any, context: Any, *, key: str = "clarify-task") -> Any:
    submission = make_submission(
        context,
        envelope=make_submission(context).envelope.model_copy(
            update={"idempotency_key": key, "request_id": key}
        ),
    )
    record = await store.create_task(submission=submission)
    attempt = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=record.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="clarify-worker",
            ttl_seconds=60,
            trace_id=context.trace_id,
        )
    )
    assert attempt.grant is not None
    return attempt.grant


async def _record(store: Any, context: Any, task_id: str) -> Any:
    return await store.get(
        lookup=TaskLookup(
            task_id=task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )


async def test_clarification_record_round_trips_and_replay_is_idempotent(
    clarification_record_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    candidate = _candidate()

    first = await clarification_record_store.save(grant=grant, candidate=candidate)
    second = await clarification_record_store.save(grant=grant, candidate=candidate)

    assert first == second
    assert await clarification_record_store.load(task_id=grant.task_id) == first
    assert first.task_id == grant.task_id
    assert first.fencing_token == grant.fencing_token
    assert first.created_at is not None


async def test_clarification_record_refuses_drift_for_same_task(
    clarification_record_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    await clarification_record_store.save(grant=grant, candidate=_candidate())

    with pytest.raises(ClarificationRecordConflictError):
            await clarification_record_store.save(
                grant=grant,
                candidate=_candidate(
                    reason_code=ClarificationReasonCode.INTERACTION_ENVIRONMENT_ASSERTION_UNCLEAR
                ),
            )


async def test_stale_grant_cannot_write_a_clarification_record(
    clarification_record_store: Any, store: Any, context: Any, clock: Any
) -> None:
    stale = await _grant(store, context)
    clock.advance(seconds=61)
    fresh = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=stale.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="fresh-worker",
            ttl_seconds=60,
            trace_id=context.trace_id,
        )
    )
    assert fresh.grant is not None

    with pytest.raises(ClarificationRecordGrantError):
        await clarification_record_store.save(grant=stale, candidate=_candidate())
    saved = await clarification_record_store.save(
        grant=fresh.grant, candidate=_candidate()
    )
    assert saved.fencing_token == fresh.grant.fencing_token


async def test_terminal_task_cannot_gain_a_late_clarification_record(
    clarification_record_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    current = await _record(store, context, grant.task_id)
    result = await store.transition(
        command=TransitionCommand(
            task_id=grant.task_id,
            expected_version=current.version,
            to_status=TaskStatus.FAILED,
            fencing_token=grant.fencing_token,
        )
    )
    assert result.applied

    with pytest.raises(ClarificationRecordStateError):
        await clarification_record_store.save(grant=grant, candidate=_candidate())


async def test_unknown_task_is_a_state_error_not_a_grant_loser(
    clarification_record_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    unknown_lease = grant.lease.model_copy(update={"task_id": "unknown-task"})
    unknown_grant = grant.model_copy(update={"lease": unknown_lease})

    with pytest.raises(ClarificationRecordStateError):
        await clarification_record_store.save(
            grant=unknown_grant, candidate=_candidate()
        )


async def test_clarification_candidate_cannot_supply_task_time_or_fence() -> None:
    fields = set(ClarificationRecordCandidate.model_fields)
    assert fields == {
        "subject",
        "reason_code",
        "missing_fields",
        "confirmed_slots",
    }
    assert {"task_id", "created_at", "fencing_token"} <= set(
        ClarificationRecord.model_fields
    )


async def test_unknown_task_has_no_clarification_record(
    clarification_record_store: Any,
) -> None:
    assert await clarification_record_store.load(task_id="unknown") is None


CLARIFICATION_RECORD_CASES = (
    test_clarification_record_round_trips_and_replay_is_idempotent,
    test_clarification_record_refuses_drift_for_same_task,
    test_stale_grant_cannot_write_a_clarification_record,
    test_terminal_task_cannot_gain_a_late_clarification_record,
    test_unknown_task_is_a_state_error_not_a_grant_loser,
    test_clarification_candidate_cannot_supply_task_time_or_fence,
    test_unknown_task_has_no_clarification_record,
)

ALL_GROUPS = {
    "clarification_records": CLARIFICATION_RECORD_CASES,
}

"""ModelArtifactStore 的内存/PostgreSQL 共享行为套件。"""

from __future__ import annotations

from typing import Any

import pytest
from tests.conftest import make_submission
from tests.suites.task_store import bind

from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    AttemptIntent,
    IntentDraft,
    IntentSource,
    ModelAdvisory,
    ModelUsage,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.persistence.model_artifacts import (
    AdvisoryArtifactCandidate,
    IntentArtifactCandidate,
    ModelArtifactConflictError,
    ModelArtifactGrantError,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand

__all__ = ["ALL_GROUPS", "MODEL_ARTIFACT_CASES", "bind"]


def _intent_candidate(**updates: object) -> IntentArtifactCandidate:
    values: dict[str, object] = {
        "draft": IntentDraft(
            intent="starrocks.slow_query.diagnose",
            slots={"window_minutes": "30"},
            missing=(),
            confidence=0.9,
            source=IntentSource.MODEL,
        ),
        "origin": "model",
        "provider": "google-gemini-developer-api",
        "model": "gemini-3-flash-preview",
        "provider_origin": "https://generativelanguage.googleapis.com",
        "prompt_revision": "ri3-intent-prompt-v1",
        "schema_revision": "ri3-intent-schema-v1",
        "input_digest": "a" * 64,
        "result_digest": "b" * 64,
        "usage": ModelUsage(input_tokens=10, output_tokens=5),
    }
    return IntentArtifactCandidate(**(values | updates))


def _advisory_candidate(**updates: object) -> AdvisoryArtifactCandidate:
    values: dict[str, object] = {
        "advisory": AdvisoryModelResult(
            advisory=ModelAdvisory(
                analysis="扫描行数偏高",
                suggestions=("检查分区裁剪",),
                uncertainties=(),
            ),
            usage=ModelUsage(input_tokens=20, output_tokens=8),
        ).advisory,
        "provider": "google-gemini-developer-api",
        "model": "gemini-3-flash-preview",
        "provider_origin": "https://generativelanguage.googleapis.com",
        "prompt_revision": "ri3-advisory-prompt-v1",
        "schema_revision": "ri3-advisory-schema-v1",
        "input_digest": "c" * 64,
        "result_digest": "d" * 64,
        "usage": ModelUsage(input_tokens=20, output_tokens=8),
    }
    return AdvisoryArtifactCandidate(**(values | updates))


async def _grant(store: Any, context: Any, *, key: str = "artifact-task") -> Any:
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
            owner="artifact-worker",
            ttl_seconds=60,
            trace_id=context.trace_id,
        )
    )
    assert attempt.grant is not None
    return attempt.grant


async def _move_to_running(store: Any, context: Any, grant: Any) -> None:
    lookup = TaskLookup(
        task_id=grant.task_id,
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
    )
    record = await store.get(lookup=lookup)
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING):
        transitioned = await store.transition(
            command=TransitionCommand(
                task_id=grant.task_id,
                expected_version=record.version,
                to_status=status,
                fencing_token=grant.fencing_token,
            )
        )
        assert transitioned.applied
        record = transitioned.winner


async def test_intent_artifact_round_trips_and_exact_replay_is_idempotent(
    model_artifact_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    candidate = _intent_candidate()

    first = await model_artifact_store.save_intent(grant=grant, candidate=candidate)
    second = await model_artifact_store.save_intent(grant=grant, candidate=candidate)

    assert first == second
    assert await model_artifact_store.load_intent(task_id=grant.task_id) == first
    assert first.task_id == grant.task_id
    assert first.fencing_token == grant.fencing_token


@pytest.mark.parametrize(
    "updates",
    [{"input_digest": "e" * 64}, {"result_digest": "f" * 64}],
)
async def test_intent_artifact_refuses_digest_drift(
    model_artifact_store: Any,
    store: Any,
    context: Any,
    updates: dict[str, object],
) -> None:
    grant = await _grant(store, context)
    original = _intent_candidate()
    await model_artifact_store.save_intent(grant=grant, candidate=original)

    with pytest.raises(ModelArtifactConflictError):
        await model_artifact_store.save_intent(
            grant=grant, candidate=_intent_candidate(**updates)
        )
    assert (await model_artifact_store.load_intent(task_id=grant.task_id)).input_digest == (
        original.input_digest
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"provider": "drifted-provider"},
        {"model": "drifted-model"},
        {"provider_origin": "https://drifted.example.invalid"},
        {"prompt_revision": "drifted-prompt"},
        {"schema_revision": "drifted-schema"},
        {"usage": ModelUsage(input_tokens=11, output_tokens=5)},
    ],
)
async def test_intent_artifact_refuses_metadata_drift_with_same_digests(
    model_artifact_store: Any,
    store: Any,
    context: Any,
    updates: dict[str, object],
) -> None:
    grant = await _grant(store, context)
    await model_artifact_store.save_intent(
        grant=grant, candidate=_intent_candidate()
    )

    with pytest.raises(ModelArtifactConflictError):
        await model_artifact_store.save_intent(
            grant=grant, candidate=_intent_candidate(**updates)
        )


async def test_stale_grant_cannot_write_an_intent_artifact(
    model_artifact_store: Any, store: Any, context: Any, clock: Any
) -> None:
    stale = await _grant(store, context)
    clock.advance(seconds=61)
    fresh_attempt = await store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=stale.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="fresh-worker",
            ttl_seconds=60,
            trace_id=context.trace_id,
        )
    )
    assert fresh_attempt.grant is not None

    with pytest.raises(ModelArtifactGrantError):
        await model_artifact_store.save_intent(
            grant=stale, candidate=_intent_candidate()
        )
    saved = await model_artifact_store.save_intent(
        grant=fresh_attempt.grant, candidate=_intent_candidate()
    )
    assert saved.fencing_token == fresh_attempt.grant.fencing_token


async def test_terminal_task_cannot_gain_a_late_intent_artifact(
    model_artifact_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    record = await store.get(
        lookup=TaskLookup(
            task_id=grant.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    result = await store.transition(
        command=TransitionCommand(
            task_id=grant.task_id,
            expected_version=record.version,
            to_status=TaskStatus.FAILED,
            fencing_token=grant.fencing_token,
        )
    )
    assert result.applied

    with pytest.raises(ModelArtifactGrantError):
        await model_artifact_store.save_intent(
            grant=grant, candidate=_intent_candidate()
        )


async def test_advisory_requires_running_and_round_trips(
    model_artifact_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    await _move_to_running(store, context, grant)

    candidate = _advisory_candidate()
    saved = await model_artifact_store.save_advisory(
        grant=grant, candidate=candidate
    )
    assert await model_artifact_store.load_advisory(task_id=grant.task_id) == saved
    assert saved.advisory == candidate.advisory


async def test_advisory_exact_replay_is_idempotent_and_drift_is_rejected(
    model_artifact_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    await _move_to_running(store, context, grant)
    candidate = _advisory_candidate()

    first = await model_artifact_store.save_advisory(
        grant=grant, candidate=candidate
    )
    second = await model_artifact_store.save_advisory(
        grant=grant, candidate=candidate
    )
    assert second == first

    with pytest.raises(ModelArtifactConflictError):
        await model_artifact_store.save_advisory(
            grant=grant,
            candidate=_advisory_candidate(input_digest="e" * 64),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"provider": "drifted-provider"},
        {"model": "drifted-model"},
        {"provider_origin": "https://drifted.example.invalid"},
        {"prompt_revision": "drifted-prompt"},
        {"schema_revision": "drifted-schema"},
        {"usage": ModelUsage(input_tokens=21, output_tokens=8)},
    ],
)
async def test_advisory_refuses_metadata_drift_with_same_digests(
    model_artifact_store: Any,
    store: Any,
    context: Any,
    updates: dict[str, object],
) -> None:
    grant = await _grant(store, context)
    await _move_to_running(store, context, grant)
    await model_artifact_store.save_advisory(
        grant=grant, candidate=_advisory_candidate()
    )

    with pytest.raises(ModelArtifactConflictError):
        await model_artifact_store.save_advisory(
            grant=grant, candidate=_advisory_candidate(**updates)
        )


async def test_stale_grant_cannot_write_an_advisory_artifact(
    model_artifact_store: Any,
    store: Any,
    context: Any,
    clock: Any,
) -> None:
    stale = await _grant(store, context)
    await _move_to_running(store, context, stale)
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

    with pytest.raises(ModelArtifactGrantError):
        await model_artifact_store.save_advisory(
            grant=stale, candidate=_advisory_candidate()
        )
    saved = await model_artifact_store.save_advisory(
        grant=fresh.grant, candidate=_advisory_candidate()
    )
    assert saved.fencing_token == fresh.grant.fencing_token


async def test_terminal_task_cannot_gain_a_late_advisory_artifact(
    model_artifact_store: Any, store: Any, context: Any
) -> None:
    grant = await _grant(store, context)
    await _move_to_running(store, context, grant)
    lookup = TaskLookup(
        task_id=grant.task_id,
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
    )
    record = await store.get(lookup=lookup)
    result = await store.transition(
        command=TransitionCommand(
            task_id=grant.task_id,
            expected_version=record.version,
            to_status=TaskStatus.FAILED,
            fencing_token=grant.fencing_token,
        )
    )
    assert result.applied

    with pytest.raises(ModelArtifactGrantError):
        await model_artifact_store.save_advisory(
            grant=grant, candidate=_advisory_candidate()
        )


async def test_unknown_task_has_no_model_artifacts(model_artifact_store: Any) -> None:
    assert await model_artifact_store.load_intent(task_id="unknown") is None
    assert await model_artifact_store.load_advisory(task_id="unknown") is None


MODEL_ARTIFACT_CASES = (
    test_intent_artifact_round_trips_and_exact_replay_is_idempotent,
    test_intent_artifact_refuses_digest_drift,
    test_intent_artifact_refuses_metadata_drift_with_same_digests,
    test_stale_grant_cannot_write_an_intent_artifact,
    test_terminal_task_cannot_gain_a_late_intent_artifact,
    test_advisory_requires_running_and_round_trips,
    test_advisory_exact_replay_is_idempotent_and_drift_is_rejected,
    test_advisory_refuses_metadata_drift_with_same_digests,
    test_stale_grant_cannot_write_an_advisory_artifact,
    test_terminal_task_cannot_gain_a_late_advisory_artifact,
    test_unknown_task_has_no_model_artifacts,
)

ALL_GROUPS = {"model_artifacts": MODEL_ARTIFACT_CASES}

"""M5 提交事实与作用域查询契约。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    Channel,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskSubmission,
)
from xiaowei_agent.persistence.store import request_dedup_digest, submission_digest

_AS_OF = dt.datetime(2026, 9, 5, 9, 30, tzinfo=dt.UTC)


def _envelope(**updates: object) -> RequestEnvelope:
    values: dict[str, object] = {
        "request_id": "request-1",
        "tenant_id": "tenant-a",
        "actor": "alice",
        "channel": Channel.API,
        "text": "why is the query slow",
        "idempotency_key": "idem-1",
        "environment_id": "dev",
    }
    return RequestEnvelope(**(values | updates))


def _context(**updates: object) -> RequestContext:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-1",
    }
    return RequestContext(**(values | updates))


def _submission(**updates: object) -> TaskSubmission:
    values: dict[str, object] = {
        "envelope": _envelope(),
        "context": _context(),
        "as_of": _AS_OF,
    }
    return TaskSubmission(**(values | updates))


def test_submission_requires_an_aware_as_of() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _submission(as_of=_AS_OF.replace(tzinfo=None))


@pytest.mark.parametrize(
    "changed",
    [
        _submission(envelope=_envelope(text="different")),
        _submission(context=_context(trace_id="1" * 32)),
        _submission(as_of=_AS_OF + dt.timedelta(seconds=1)),
    ],
)
def test_submission_digest_covers_every_top_level_fact(changed: TaskSubmission) -> None:
    assert submission_digest(changed) != submission_digest(_submission())


def test_submission_digest_is_a_sha256_hex_checksum() -> None:
    digest = submission_digest(_submission())
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_request_dedup_digest_excludes_retry_identity_and_time() -> None:
    original = _submission()
    retry = _submission(
        envelope=_envelope(request_id="request-2"),
        context=_context(trace_id="2" * 32),
        as_of=_AS_OF + dt.timedelta(hours=1),
    )
    assert request_dedup_digest(original.envelope, original.context) == (
        request_dedup_digest(retry.envelope, retry.context)
    )


def test_request_dedup_digest_includes_channel() -> None:
    original = _submission()
    changed = _submission(envelope=_envelope(channel=Channel.CLI))
    assert request_dedup_digest(original.envelope, original.context) != (
        request_dedup_digest(changed.envelope, changed.context)
    )


def test_idempotency_key_accepts_exactly_two_hundred_characters() -> None:
    assert len(_envelope(idempotency_key="k" * 200).idempotency_key) == 200


def test_idempotency_key_rejects_two_hundred_and_one_characters() -> None:
    with pytest.raises(ValidationError, match="at most 200"):
        _envelope(idempotency_key="k" * 201)


def test_lookup_contains_only_the_storage_scope() -> None:
    assert set(TaskLookup.model_fields) == {"task_id", "tenant_id", "environment_id"}

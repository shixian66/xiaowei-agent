"""候选集必须无歧义、可确定性排序。"""

import math

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import Candidate, CandidateSet, Rejection


def _candidate(**overrides: object) -> Candidate:
    base: dict[str, object] = {
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "operation": "list_slow_queries",
        "score": 0.9,
        "match_evidence": ("domain match",),
        "required_context": ("environment_id",),
    }
    return Candidate(**(base | overrides))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -0.1, 1.1])
def test_score_must_be_a_finite_unit_interval_value(bad: float) -> None:
    with pytest.raises(ValidationError):
        _candidate(score=bad)


def test_duplicate_candidate_is_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate candidate"):
        CandidateSet(
            resolver_version="1",
            snapshot_id="s",
            items=(_candidate(), _candidate()),
            rejections=(),
        )


def test_duplicate_rejection_is_rejected() -> None:
    reject = Rejection(capability_id="c", capability_version="1.0.0", reason_code="x")
    with pytest.raises(ValidationError, match="duplicate rejection"):
        CandidateSet(
            resolver_version="1", snapshot_id="s", items=(), rejections=(reject, reject)
        )


def test_capability_cannot_be_both_candidate_and_rejection() -> None:
    reject = Rejection(
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        reason_code="x",
    )
    with pytest.raises(ValidationError, match="both candidate and rejection"):
        CandidateSet(
            resolver_version="1",
            snapshot_id="s",
            items=(_candidate(),),
            rejections=(reject,),
        )


def test_same_capability_with_different_operations_is_allowed() -> None:
    got = CandidateSet(
        resolver_version="1",
        snapshot_id="s",
        items=(_candidate(), _candidate(operation="describe_profile")),
        rejections=(),
    )
    assert len(got.items) == 2

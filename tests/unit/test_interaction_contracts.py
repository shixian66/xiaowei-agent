"""I1 交互分类契约：模型候选不可信，Router 才能裁决。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionSource,
    RoutingDisposition,
)


def _slow_query_draft() -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"window_minutes": "30"},
        missing=(),
        confidence=0.9,
        source=IntentSource.MODEL,
    )


def test_interaction_draft_preserves_inconsistent_untrusted_candidate() -> None:
    draft = InteractionDraft(
        proposed_kind=InteractionKind.CONVERSATION,
        capability_draft=_slow_query_draft(),
        confidence=1.0,
        source=InteractionSource.MODEL,
    )

    assert draft.capability_draft is not None
    assert draft.proposed_kind is InteractionKind.CONVERSATION


def test_interaction_draft_is_strict_frozen_and_extra_forbid() -> None:
    draft = InteractionDraft(
        proposed_kind=InteractionKind.CAPABILITY_REQUEST,
        capability_draft=_slow_query_draft(),
        confidence=0.5,
        source=InteractionSource.FAKE,
    )

    with pytest.raises(ValidationError):
        draft.model_copy(update={"confidence": "0.5"})
    with pytest.raises(ValidationError):
        InteractionDraft(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            capability_draft=_slow_query_draft(),
            confidence=0.5,
            source=InteractionSource.FAKE,
            tool="must-not-exist",
        )


def test_interaction_draft_rejects_router_disposition_from_untrusted_candidate() -> None:
    with pytest.raises(ValidationError):
        InteractionDraft(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            routing_disposition=RoutingDisposition.PROCEED,
            capability_draft=_slow_query_draft(),
            confidence=0.5,
            source=InteractionSource.MODEL,
        )


def test_interaction_closed_sets_are_exact() -> None:
    assert {item.value for item in InteractionKind} == {
        "conversation",
        "knowledge_lookup",
        "log_analysis",
        "capability_request",
        "unknown",
    }
    assert {item.value for item in RoutingDisposition} == {
        "proceed",
        "respond",
        "clarify",
        "refuse",
    }

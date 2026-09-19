"""I1 interaction 分类后的确定性路由裁决。"""

import pytest

from xiaowei_agent.application.interaction_router import route_interaction
from xiaowei_agent.contracts import (
    ClarificationReasonCode,
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionRejectionReasonCode,
    InteractionSource,
    RouteSubject,
    RoutingDisposition,
)


def _capability(**slots: str) -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"window_minutes": "30"} | slots,
        missing=(),
        confidence=0.8,
        source=IntentSource.MODEL,
    )


def _draft(
    kind: InteractionKind,
    *,
    capability: IntentDraft | None = None,
) -> InteractionDraft:
    return InteractionDraft(
        proposed_kind=kind,
        capability_draft=capability,
        confidence=0.8,
        source=InteractionSource.MODEL,
    )


@pytest.mark.parametrize(
    "kind",
    [
        InteractionKind.CONVERSATION,
        InteractionKind.KNOWLEDGE_LOOKUP,
        InteractionKind.LOG_ANALYSIS,
    ],
)
def test_non_capability_routes_are_refused_before_resolver(
    kind: InteractionKind, context: object
) -> None:
    decision = route_interaction(draft=_draft(kind), context=context)

    assert decision.disposition is RoutingDisposition.REFUSE
    assert decision.reason_code is InteractionRejectionReasonCode.ROUTE_NOT_AVAILABLE
    assert decision.intent_draft is None
    assert decision.subject is None


def test_unknown_route_requires_one_to_one_clarification(context: object) -> None:
    decision = route_interaction(
        draft=_draft(InteractionKind.UNKNOWN),
        context=context,
    )

    assert decision.disposition is RoutingDisposition.CLARIFY
    assert decision.reason_code is ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS
    assert decision.intent_draft is None
    assert decision.subject == RouteSubject(
        kind="route",
        proposed_kind=InteractionKind.UNKNOWN,
    )


def test_rule_unknown_route_stays_a_preplan_rejection(context: object) -> None:
    decision = route_interaction(
        draft=InteractionDraft(
            proposed_kind=InteractionKind.UNKNOWN,
            capability_draft=None,
            confidence=0.0,
            source=InteractionSource.RULE,
        ),
        context=context,
    )

    assert decision.disposition is RoutingDisposition.REFUSE
    assert decision.reason_code is InteractionRejectionReasonCode.ROUTE_NOT_AVAILABLE
    assert decision.intent_draft is None
    assert decision.subject is None


def test_non_capability_route_cannot_smuggle_capability_draft(context: object) -> None:
    decision = route_interaction(
        draft=_draft(InteractionKind.CONVERSATION, capability=_capability()),
        context=context,
    )

    assert decision.disposition is RoutingDisposition.REFUSE
    assert (
        decision.reason_code
        is InteractionRejectionReasonCode.CAPABILITY_DRAFT_FORBIDDEN
    )
    assert decision.intent_draft is None


def test_capability_route_requires_capability_draft(context: object) -> None:
    decision = route_interaction(
        draft=_draft(InteractionKind.CAPABILITY_REQUEST),
        context=context,
    )

    assert decision.disposition is RoutingDisposition.REFUSE
    assert (
        decision.reason_code is InteractionRejectionReasonCode.CAPABILITY_DRAFT_MISSING
    )
    assert decision.intent_draft is None


def test_capability_route_proceeds_when_environment_is_absent_or_consistent(
    context: object,
) -> None:
    without_env = _capability()
    with_matching_env = _capability(environment_id="dev")

    assert (
        route_interaction(
            draft=_draft(InteractionKind.CAPABILITY_REQUEST, capability=without_env),
            context=context,
        ).intent_draft
        == without_env
    )
    decision = route_interaction(
        draft=_draft(InteractionKind.CAPABILITY_REQUEST, capability=with_matching_env),
        context=context,
    )

    assert decision.disposition is RoutingDisposition.PROCEED
    assert decision.reason_code is None
    assert decision.intent_draft == with_matching_env


def test_capability_route_refuses_environment_context_mismatch(context: object) -> None:
    decision = route_interaction(
        draft=_draft(
            InteractionKind.CAPABILITY_REQUEST,
            capability=_capability(environment_id="prod"),
        ),
        context=context,
    )

    assert decision.disposition is RoutingDisposition.REFUSE
    assert (
        decision.reason_code
        is InteractionRejectionReasonCode.ENVIRONMENT_CONTEXT_MISMATCH
    )
    assert decision.intent_draft is None

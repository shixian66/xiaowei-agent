"""I1 interaction draft 的确定性路由裁决。"""

from __future__ import annotations

from dataclasses import dataclass

from xiaowei_agent.contracts import (
    ClarificationReasonCode,
    IntentDraft,
    InteractionDraft,
    InteractionKind,
    InteractionRejectionReasonCode,
    InteractionSource,
    RequestContext,
    RouteSubject,
    RoutingDisposition,
)


@dataclass(frozen=True, slots=True)
class InteractionRouteDecision:
    """Router 输出的窄裁决；执行链只消费这里的 intent。"""

    disposition: RoutingDisposition
    reason_code: ClarificationReasonCode | InteractionRejectionReasonCode | None
    intent_draft: IntentDraft | None = None
    subject: RouteSubject | None = None


def _refuse(reason: InteractionRejectionReasonCode) -> InteractionRouteDecision:
    return InteractionRouteDecision(
        disposition=RoutingDisposition.REFUSE,
        reason_code=reason,
    )


def _clarify_route(kind: InteractionKind) -> InteractionRouteDecision:
    return InteractionRouteDecision(
        disposition=RoutingDisposition.CLARIFY,
        reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
        subject=RouteSubject(kind="route", proposed_kind=kind),
    )


def _respond() -> InteractionRouteDecision:
    return InteractionRouteDecision(
        disposition=RoutingDisposition.RESPOND,
        reason_code=None,
    )


def _proceed(draft: IntentDraft) -> InteractionRouteDecision:
    return InteractionRouteDecision(
        disposition=RoutingDisposition.PROCEED,
        reason_code=None,
        intent_draft=draft,
    )


def route_interaction(
    *, draft: InteractionDraft, context: RequestContext
) -> InteractionRouteDecision:
    """在 resolver 之前裁决 interaction 类型与环境断言。

    `context.environment_id` 是目标环境事实；模型/规则草案里的同名槽位只作为用户
    断言复核，不能反向改写上下文。
    """

    if (
        draft.proposed_kind is not InteractionKind.CAPABILITY_REQUEST
        and draft.capability_draft is not None
    ):
        return _refuse(InteractionRejectionReasonCode.CAPABILITY_DRAFT_FORBIDDEN)

    if (
        draft.proposed_kind is InteractionKind.UNKNOWN
        and draft.source is InteractionSource.MODEL
    ):
        return _clarify_route(draft.proposed_kind)

    if draft.proposed_kind is InteractionKind.CONVERSATION:
        return _respond()

    if draft.proposed_kind is not InteractionKind.CAPABILITY_REQUEST:
        return _refuse(InteractionRejectionReasonCode.ROUTE_NOT_AVAILABLE)

    if draft.capability_draft is None:
        return _refuse(InteractionRejectionReasonCode.CAPABILITY_DRAFT_MISSING)

    asserted_environment = draft.capability_draft.slots.get("environment_id")
    if (
        asserted_environment is not None
        and asserted_environment != context.environment_id
    ):
        return _refuse(InteractionRejectionReasonCode.ENVIRONMENT_CONTEXT_MISMATCH)

    return _proceed(draft.capability_draft)


__all__ = [
    "InteractionRouteDecision",
    "route_interaction",
]

"""I1 入口交互分类契约。"""

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, FiniteFloat
from xiaowei_agent.contracts.enums import (
    InteractionKind,
    InteractionSource,
    RoutingDisposition,
)
from xiaowei_agent.contracts.intent import IntentDraft


class InteractionDraft(Contract):
    """不可信交互候选；最终处置由确定性 Router 裁决。"""

    proposed_kind: InteractionKind
    routing_disposition: RoutingDisposition
    capability_draft: IntentDraft | None = None
    confidence: FiniteFloat = Field(ge=0.0, le=1.0)
    source: InteractionSource

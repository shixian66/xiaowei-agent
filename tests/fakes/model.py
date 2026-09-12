"""两个窄模型端口的离线 fake。"""

from __future__ import annotations

from xiaowei_agent.application.model_ports import validate_advisory_output_tokens
from xiaowei_agent.contracts import (
    IntentDraft,
    ModelAdvisory,
    ModelIntentRequest,
    SlowQueryAdvisoryRequest,
    StrictInt,
)


class ScriptedModelAdapter:
    def __init__(self, *, intent: IntentDraft, advisory: ModelAdvisory) -> None:
        self.intent = intent
        self.advisory = advisory
        self.intent_requests: list[ModelIntentRequest] = []
        self.advisory_requests: list[tuple[SlowQueryAdvisoryRequest, int]] = []

    async def generate_intent(self, request: ModelIntentRequest) -> IntentDraft:
        self.intent_requests.append(request)
        return self.intent

    async def generate_advisory(
        self,
        request: SlowQueryAdvisoryRequest,
        *,
        max_output_tokens: StrictInt,
    ) -> ModelAdvisory:
        limit = validate_advisory_output_tokens(max_output_tokens)
        self.advisory_requests.append((request, limit))
        return self.advisory


__all__ = ["ScriptedModelAdapter"]

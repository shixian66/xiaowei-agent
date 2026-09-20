"""I1 L0 eval: interaction safety invariants are deterministic."""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.admission import CONTEXT
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.interaction_router import route_interaction
from xiaowei_agent.contracts import (
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelUsage,
    TaskStatus,
)

pytestmark = pytest.mark.security

_FIXTURE = Path(__file__).parent / "fixtures" / "i1_interaction_cases.json"


class _InteractionPort:
    def __init__(self, draft: InteractionDraft) -> None:
        self.draft = draft
        self.calls = 0

    async def classify(self, request: object) -> InteractionModelResult:
        del request
        self.calls += 1
        return InteractionModelResult(draft=self.draft, usage=ModelUsage())


def _load_cases() -> list[dict[str, Any]]:
    assert _FIXTURE.exists(), f"missing I1 eval fixture: {_FIXTURE}"
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    return list(data["cases"])


def _by(category: str) -> list[dict[str, Any]]:
    return [case for case in _load_cases() if case["category"] == category]


def _text(case: dict[str, Any]) -> str:
    if "text" in case:
        return case["text"]
    return "".join(case["text_parts"])


def _draft(payload: dict[str, Any]) -> InteractionDraft:
    capability = payload.get("capability")
    return InteractionDraft(
        proposed_kind=InteractionKind(payload["kind"]),
        capability_draft=(
            None
            if capability is None
            else IntentDraft(
                intent=capability["intent"],
                slots=capability.get("slots", {}),
                missing=tuple(capability.get("missing", ())),
                confidence=capability.get("confidence", 0.8),
                source=IntentSource(capability.get("source", "model")),
            )
        ),
        confidence=payload.get("confidence", 0.8),
        source=InteractionSource(payload.get("source", "model")),
    )


def test_fixture_covers_the_i1_l0_safety_matrix() -> None:
    tags = {tag for case in _load_cases() for tag in case.get("tags", [])}
    assert {
        "kind_conversation",
        "kind_knowledge_lookup",
        "kind_log_analysis",
        "kind_unknown",
        "kind_capability_request",
        "environment_conflict",
        "log_injection",
        "pseudo_readonly_write",
        "restricted_select",
        "model_slot_pollution",
        "secret_shaped_input",
    } <= tags


@pytest.mark.parametrize("case", _by("l0_router"), ids=lambda case: case["id"])
def test_router_safety_cases_have_closed_dispositions(case: dict[str, Any]) -> None:
    decision = route_interaction(draft=_draft(case["draft"]), context=CONTEXT)

    assert decision.disposition.value == case["expected_disposition"]
    assert (
        None if decision.reason_code is None else decision.reason_code.value
    ) == case.get("expected_reason")
    assert (decision.intent_draft is not None) == case.get("expects_intent", False)


@pytest.mark.parametrize(
    "case", _by("l0_unsupported_runtime"), ids=lambda case: case["id"]
)
async def test_unsupported_i1_inputs_stop_before_gateway(case: dict[str, Any]) -> None:
    harness = RuntimeHarness(GOLDEN)

    payload = await harness.handle(_text(case), idempotency_key=case["id"])

    assert payload.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0


@pytest.mark.parametrize(
    "case", _by("l0_model_slot_pollution"), ids=lambda case: case["id"]
)
async def test_model_slots_do_not_become_executable_params(
    case: dict[str, Any],
) -> None:
    port = _InteractionPort(_draft(case["draft"]))
    harness = RuntimeHarness(GOLDEN, interaction_classifier=port)

    payload = await harness.handle(case["text"], idempotency_key=case["id"])

    assert payload.status is TaskStatus.SUCCEEDED
    assert port.calls == 1
    stored = await harness.plan_store.load(task_id=harness.task_id)
    forbidden = case["forbidden_typed_argument"]
    for step in stored.plan.steps:
        assert step.typed_arguments.get(forbidden["name"]) != forbidden["value"]

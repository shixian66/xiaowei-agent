"""I1 L0 eval: interaction safety invariants are deterministic."""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.admission import CONTEXT
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.capability_runtime import CapabilityBindingRegistry
from xiaowei_agent.application.default_capabilities import (
    ASSET_INVENTORY_BINDING,
    PROMETHEUS_ALERT_BINDING,
    SLOW_QUERY_BINDING,
)
from xiaowei_agent.application.interaction_router import route_interaction
from xiaowei_agent.capabilities.specs import OP_LIST
from xiaowei_agent.contracts import (
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelUsage,
    ReadClass,
    TaskStatus,
)

pytestmark = pytest.mark.security

_FIXTURE = Path(__file__).parent / "fixtures" / "i1_interaction_cases.json"
_CONTROL_TEXT = "asdfghjkl qwertyuiop"


class _InteractionPort:
    def __init__(self, draft: InteractionDraft) -> None:
        self.draft = draft
        self.calls = 0

    async def classify(self, request: object) -> InteractionModelResult:
        del request
        self.calls += 1
        return InteractionModelResult(draft=self.draft, usage=ModelUsage())


class _TextAwareInteractionPort:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.calls = 0
        self.requests: list[Any] = []

    async def classify(self, request: Any) -> InteractionModelResult:
        self.calls += 1
        self.requests.append(request)
        text = str(request.user_text)
        markers = self.case.get("classifier_text_markers", ())
        if markers and all(marker in text for marker in markers):
            return InteractionModelResult(
                draft=_draft(self.case["draft"]), usage=ModelUsage()
            )
        return InteractionModelResult(draft=_unknown_model_draft(), usage=ModelUsage())


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


def _unknown_model_draft() -> InteractionDraft:
    return InteractionDraft(
        proposed_kind=InteractionKind.UNKNOWN,
        capability_draft=None,
        confidence=0.0,
        source=InteractionSource.MODEL,
    )


def _restrict_list_operations(harness: RuntimeHarness) -> None:
    snapshot = harness.runtime._snapshot
    restricted_specs = []
    for spec in snapshot.specs:
        operations = tuple(
            operation.model_copy(update={"read_class": ReadClass.RESTRICTED})
            if operation.operation == OP_LIST
            else operation
            for operation in spec.operations
        )
        restricted_specs.append(spec.model_copy(update={"operations": operations}))
    restricted_snapshot = snapshot.model_copy(update={"specs": tuple(restricted_specs)})
    harness.runtime._snapshot = restricted_snapshot
    harness.runtime._bindings = CapabilityBindingRegistry(
        snapshot=restricted_snapshot,
        policy_snapshot=harness.runtime._runner._policy_snapshot,
        bindings=(SLOW_QUERY_BINDING, PROMETHEUS_ALERT_BINDING, ASSET_INVENTORY_BINDING),
    )


def _unsupported_runtime(case: dict[str, Any]) -> tuple[RuntimeHarness, _TextAwareInteractionPort]:
    port = _TextAwareInteractionPort(case)
    tags = set(case.get("tags", ()))
    harness = RuntimeHarness(
        GOLDEN,
        interaction_classifier=port,
        synthetic_write="pseudo_readonly_write" in tags,
    )
    if "restricted_select" in tags:
        _restrict_list_operations(harness)
    return harness, port


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
    harness, port = _unsupported_runtime(case)

    payload = await harness.handle(_text(case), idempotency_key=case["id"])
    record = await harness.store.get(lookup=harness.lookup)
    stages = [event.stage.value for event in harness.sink.events]
    route_reason = case.get("expected_route_reason")

    assert payload.status.value == case["expected_status"]
    assert record.terminal_reason == case["expected_terminal_reason"]
    if route_reason is not None:
        decision = route_interaction(draft=_draft(case["draft"]), context=CONTEXT)
        assert (
            None if decision.reason_code is None else decision.reason_code.value
        ) == route_reason
    for stage in case.get("expected_stage_contains", ()):
        assert stage in stages
    for stage in case.get("expected_stage_excludes", ()):
        assert stage not in stages
    assert harness.gateway.invocations == case["expected_gateway_calls"]
    assert harness.approval_gate.calls == case.get("expected_approval_calls", 0)
    assert port.calls == 1
    for parts in case.get("expected_model_text_excludes_parts", ()):
        assert "".join(parts) not in str(port.requests[-1].user_text)


@pytest.mark.parametrize(
    "case", _by("l0_unsupported_runtime"), ids=lambda case: case["id"]
)
async def test_unsupported_i1_inputs_are_distinct_from_unknown_text(
    case: dict[str, Any],
) -> None:
    case_harness, _ = _unsupported_runtime(case)
    control_harness, _ = _unsupported_runtime(case)

    await case_harness.handle(_text(case), idempotency_key=f"{case['id']}-case")
    await control_harness.handle(
        _CONTROL_TEXT, idempotency_key=f"{case['id']}-control"
    )
    case_record = await case_harness.store.get(lookup=case_harness.lookup)
    control_record = await control_harness.store.get(lookup=control_harness.lookup)

    assert (
        case_record.status,
        case_record.terminal_reason,
        tuple(event.stage for event in case_harness.sink.events),
    ) != (
        control_record.status,
        control_record.terminal_reason,
        tuple(event.stage for event in control_harness.sink.events),
    )


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

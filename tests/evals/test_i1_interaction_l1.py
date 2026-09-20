"""I1 L1 eval: classifier, resolver and slot clarification behavior."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.admission import CONTEXT
from tests.fakes.asset_recordings import recording_for as asset_recording_for
from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    AttemptIntent,
    ClarificationField,
    TaskStatus,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingAdapter
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingAdapter

_FIXTURE = Path(__file__).parent / "fixtures" / "i1_interaction_cases.json"
_SNAPSHOT = StaticCapabilityRegistry().snapshot()
_CAPABILITY_AS_OF = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


def _load_cases() -> list[dict[str, Any]]:
    assert _FIXTURE.exists(), f"missing I1 eval fixture: {_FIXTURE}"
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    return list(data["cases"])


def _by(category: str) -> list[dict[str, Any]]:
    return [case for case in _load_cases() if case["category"] == category]


def _runtime_for(case: dict[str, Any]) -> RuntimeHarness:
    capability = case["expected_capability"]
    if capability == "starrocks.slow_query.diagnose":
        return RuntimeHarness(GOLDEN)
    if capability == "prometheus.alert.evidence":
        alerts, metrics = recordings_for("golden")
        return RuntimeHarness(
            None,
            adapters={
                "alertmanager": AlertmanagerRecordingAdapter(alerts),
                "prometheus": PrometheusRecordingAdapter(metrics),
            },
            as_of=_CAPABILITY_AS_OF,
        )
    if capability == "asset.inventory.lookup":
        return RuntimeHarness(
            None,
            adapters={
                "asset_inventory": AssetInventoryRecordingAdapter(
                    asset_recording_for("golden"),
                    operation="lookup_asset",
                )
            },
            as_of=_CAPABILITY_AS_OF,
        )
    raise AssertionError(f"unknown capability in fixture: {capability}")


async def _execute_created(harness: RuntimeHarness, task_id: str) -> TaskStatus:
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None
    outcome = await harness.runtime.execute_task(
        grant=attempt.grant,
        submission=attempt.submission,
    )
    return outcome.status


def test_fixture_covers_the_i1_l1_interaction_matrix() -> None:
    tags = {tag for case in _load_cases() for tag in case.get("tags", [])}
    assert {
        "capability_slow_query",
        "capability_prometheus",
        "capability_asset",
        "synonym",
        "typo_near_miss",
        "environment_match",
        "environment_ambiguous",
        "missing_slot",
        "clarification_parent_chain",
        "slot_value_change",
        "multi_turn",
    } <= tags


@pytest.mark.parametrize("case", _by("l1_classifier"), ids=lambda case: case["id"])
def test_classifier_and_resolver_expectations_are_stable(
    case: dict[str, Any],
) -> None:
    draft = RuleBasedIntentInterpreter().interpret(text=case["text"], context=CONTEXT)
    candidates = DeterministicCapabilityResolver().resolve(
        draft=draft,
        context=CONTEXT,
        snapshot=_SNAPSHOT,
    )

    assert draft.intent == case["expected_intent"]
    assert dict(draft.slots) == case.get("expected_slots", {})
    assert {item.capability_id for item in candidates.items} == set(case[
        "expected_capabilities"
    ])


@pytest.mark.parametrize("case", _by("l1_runtime_success"), ids=lambda case: case["id"])
async def test_current_three_capabilities_reach_expected_terminal_state(
    case: dict[str, Any],
) -> None:
    harness = _runtime_for(case)

    payload = await harness.handle(case["text"], idempotency_key=case["id"])

    assert payload.status.value == case["expected_status"]
    assert harness.gateway.invocations == case["expected_gateway_calls"]


@pytest.mark.parametrize(
    "case", _by("l1_clarification_flow"), ids=lambda case: case["id"]
)
async def test_clarification_parent_chain_reaches_ready_child(
    case: dict[str, Any],
) -> None:
    harness = _runtime_for(case)

    parent = await harness.runtime.submit_task(
        submission=harness.submission(case["text"], idempotency_key=case["id"])
    )
    parent_status = await _execute_created(harness, parent.task_id)
    saved = await harness.clarification_records.load(task_id=parent.task_id)
    assert parent_status is TaskStatus.CLARIFICATION_REQUIRED
    assert saved is not None
    assert tuple(field.value for field in saved.missing_fields) == tuple(
        case["expected_missing_fields"]
    )

    child_submission = harness.submission(
        case["child_text"],
        idempotency_key=f"{case['id']}-child",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id
    child_status = await _execute_created(harness, child.task_id)

    assert child_status.value == case["expected_child_status"]
    assert await harness.clarification_records.load(task_id=child.task_id) is None
    if case["expected_capability"] == "prometheus.alert.evidence":
        assert ClarificationField.INSTANCE.value in case["expected_missing_fields"]

"""M6a Prometheus L1：意图、缺槽与污染边界。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.admission import CONTEXT

from xiaowei_agent.application.capability_runtime import CapabilityPreparationError
from xiaowei_agent.application.default_capabilities import PROMETHEUS_ALERT_BINDING
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.prometheus_alert import PROMETHEUS_ALERT_CAPABILITY_ID
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m6a_prometheus_l1.json").read_text(
        encoding="utf-8"
    )
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_SNAPSHOT = StaticCapabilityRegistry().snapshot()


def _by(category: str) -> list[dict[str, Any]]:
    return [case for case in _CASES if case["category"] == category]


def _draft(text: str):
    return RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)


def test_corpus_has_exact_declared_shape() -> None:
    categories = ("golden", "near_miss", "missing", "invalid", "pollution")
    assert {name: len(_by(name)) for name in categories} == {
        "golden": 6,
        "near_miss": 6,
        "missing": 3,
        "invalid": 1,
        "pollution": 4,
    }


@pytest.mark.parametrize("case", _by("golden"), ids=lambda case: case["id"])
def test_golden_cases_resolve_with_exact_slots(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    assert draft.intent == PROMETHEUS_ALERT_CAPABILITY_ID
    assert draft.missing == ()
    for key, value in case["slots"].items():
        assert draft.slots[key] == value
    selected = DeterministicCapabilityResolver().resolve(
        draft=draft, context=CONTEXT, snapshot=_SNAPSHOT
    )
    assert {item.capability_id for item in selected.items} == {
        PROMETHEUS_ALERT_CAPABILITY_ID
    }


@pytest.mark.parametrize("case", _by("near_miss"), ids=lambda case: case["id"])
def test_near_miss_cases_do_not_route_to_prometheus(case: dict[str, Any]) -> None:
    assert _draft(case["text"]).intent != PROMETHEUS_ALERT_CAPABILITY_ID


def test_reading_a_silenced_alert_is_not_mistaken_for_a_write_request() -> None:
    draft = _draft(
        "查看已静默告警 HostHighCpu 在 node-1.example.com:9100 的证据"
    )
    assert draft.intent == PROMETHEUS_ALERT_CAPABILITY_ID


@pytest.mark.parametrize("case", _by("missing"), ids=lambda case: case["id"])
def test_missing_cases_never_invent_required_slots(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    assert draft.intent == PROMETHEUS_ALERT_CAPABILITY_ID
    assert set(case["missing"]) <= set(draft.missing)


@pytest.mark.parametrize("case", _by("invalid"), ids=lambda case: case["id"])
def test_invalid_window_is_rejected_by_the_planner(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=draft, context=CONTEXT, snapshot=_SNAPSHOT)
        .items
        if item.operation == PROMETHEUS_ALERT_BINDING.entry_operation
    )
    with pytest.raises(CapabilityPreparationError):
        PROMETHEUS_ALERT_BINDING.planner(
            candidate=candidate,
            draft=draft,
            context=CONTEXT,
            as_of=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
            snapshot=_SNAPSHOT,
        )


@pytest.mark.parametrize("case", _by("pollution"), ids=lambda case: case["id"])
def test_polluted_execution_slots_never_enter_the_draft(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    assert draft.intent == PROMETHEUS_ALERT_CAPABILITY_ID
    for key in case["forbidden"]:
        assert key not in draft.slots

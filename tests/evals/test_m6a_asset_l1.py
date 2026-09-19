"""M6a 资产 L1：意图、缺槽、规范化与污染边界。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.admission import CONTEXT

from xiaowei_agent.application.capability_input import SlotInvalid, SlotReady
from xiaowei_agent.application.default_capabilities import ASSET_INVENTORY_BINDING
from xiaowei_agent.capabilities.asset_inventory import ASSET_INVENTORY_CAPABILITY_ID
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m6a_asset_l1.json").read_text(
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
    categories = ("golden", "near_miss", "missing", "pollution")
    assert {name: len(_by(name)) for name in categories} == {
        "golden": 6,
        "near_miss": 6,
        "missing": 4,
        "pollution": 4,
    }


@pytest.mark.parametrize("case", _by("golden"), ids=lambda case: case["id"])
def test_golden_cases_resolve_and_normalise_exact_selector(
    case: dict[str, Any],
) -> None:
    draft = _draft(case["text"])
    assert draft.intent == ASSET_INVENTORY_CAPABILITY_ID
    assert draft.missing == ()
    selected = DeterministicCapabilityResolver().resolve(
        draft=draft, context=CONTEXT, snapshot=_SNAPSHOT
    )
    candidate = next(
        item
        for item in selected.items
        if item.operation == ASSET_INVENTORY_BINDING.entry_operation
    )
    verified = ASSET_INVENTORY_BINDING.input_binding.slot_verifier(
        candidate=candidate,
        draft=draft,
        context=CONTEXT,
        as_of=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
        user_text=case["text"],
    )
    assert isinstance(verified, SlotReady)
    prepared = ASSET_INVENTORY_BINDING.input_binding.planner(
        candidate=candidate,
        params=verified.params,
        context=CONTEXT,
        snapshot=_SNAPSHOT,
    )
    assert prepared.target.resource_ids == (
        f"{case['selector_kind']}:{case['selector_value']}",
    )


@pytest.mark.parametrize("case", _by("near_miss"), ids=lambda case: case["id"])
def test_near_miss_cases_do_not_route_to_asset_lookup(case: dict[str, Any]) -> None:
    assert _draft(case["text"]).intent != ASSET_INVENTORY_CAPABILITY_ID


@pytest.mark.parametrize("case", _by("missing"), ids=lambda case: case["id"])
def test_missing_cases_never_invent_a_selector(case: dict[str, Any]) -> None:
    draft = _draft(case["text"])
    assert draft.intent == ASSET_INVENTORY_CAPABILITY_ID
    if case["mode"] == "absent":
        assert draft.missing == ("asset_selector",)
        assert draft.slots == {}
        return
    selected = DeterministicCapabilityResolver().resolve(
        draft=draft, context=CONTEXT, snapshot=_SNAPSHOT
    )
    candidate = next(
        item
        for item in selected.items
        if item.operation == ASSET_INVENTORY_BINDING.entry_operation
    )
    result = ASSET_INVENTORY_BINDING.input_binding.slot_verifier(
        candidate=candidate,
        draft=draft,
        context=CONTEXT,
        as_of=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
        user_text=case["text"],
    )
    assert isinstance(result, SlotInvalid)


@pytest.mark.parametrize("case", _by("pollution"), ids=lambda case: case["id"])
def test_polluted_execution_slots_never_enter_asset_draft(
    case: dict[str, Any],
) -> None:
    draft = _draft(case["text"])
    assert draft.intent == ASSET_INVENTORY_CAPABILITY_ID
    for key in case["forbidden"]:
        assert key not in draft.slots

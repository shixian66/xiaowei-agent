"""M6a 资产 L2：单步只读闭环真值表。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.asset_recordings import recording_for
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import TaskStatus
from xiaowei_agent.planning import compute_plan_hash, compute_tool_call_hash
from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingAdapter

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m6a_asset_l2.json").read_text(
        encoding="utf-8"
    )
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
_TEXT = "查资产 hostname=node-1.example.com"


def _harness(scenario: str) -> RuntimeHarness:
    return RuntimeHarness(
        None,
        adapters={
            "asset_inventory": AssetInventoryRecordingAdapter(
                recording_for(scenario), operation="lookup_asset"
            )
        },
        as_of=_AT,
    )


def test_corpus_has_ten_executed_cases() -> None:
    assert len(_CASES) == 10
    assert len({case["id"] for case in _CASES}) == len(_CASES)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
async def test_each_corpus_case_executes_the_real_runtime(case: dict[str, Any]) -> None:
    harness = _harness(case["scenario"])
    payload = await harness.handle(case.get("text", _TEXT))
    assert payload.status is TaskStatus(case["status"])
    assert harness.adapters["asset_inventory"].call_count == case["calls"]


async def test_repeat_is_stable_and_idempotent() -> None:
    harness = _harness("golden")
    first = await harness.handle(_TEXT)
    plan = (await harness.plan_store.load(task_id=harness.task_id)).plan
    plan_hash = compute_plan_hash(plan)
    call_hashes = tuple(compute_tool_call_hash(call) for call in harness.calls)

    second = await harness.handle(_TEXT)

    assert second == first
    assert harness.gateway.invocations == 1
    assert compute_plan_hash(
        (await harness.plan_store.load(task_id=harness.task_id)).plan
    ) == plan_hash
    assert tuple(compute_tool_call_hash(call) for call in harness.calls) == call_hashes


async def test_unique_asset_render_contains_only_the_declared_summary() -> None:
    harness = _harness("golden")
    payload = await harness.handle(_TEXT)
    body = payload.model_dump_json()
    assert "唯一资产" in body
    assert "node-1.example.com" in body
    assert "健康" not in body
    assert "拓扑" not in body


async def test_external_fields_are_not_present_in_the_result() -> None:
    harness = _harness("polluted")
    payload = await harness.handle(_TEXT)
    marker = "to" + "ken" + "=" + "synthetic-value"
    assert marker not in payload.model_dump_json()
    assert "must-not-render" not in payload.model_dump_json()

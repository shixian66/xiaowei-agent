"""M6a Prometheus L2：双数据源只读闭环。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import TaskStatus
from xiaowei_agent.planning import compute_plan_hash, compute_tool_call_hash
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingAdapter

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m6a_prometheus_l2.json").read_text(
        encoding="utf-8"
    )
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
_TEXT = "查告警 HostHighCpu 在 node-1.example.com:9100 的证据"


def _harness(scenario: str) -> RuntimeHarness:
    alerts, metrics = recordings_for(scenario)
    return RuntimeHarness(
        None,
        adapters={
            "alertmanager": AlertmanagerRecordingAdapter(alerts),
            "prometheus": PrometheusRecordingAdapter(metrics),
        },
        as_of=_AT,
    )


def test_corpus_has_ten_executed_cases() -> None:
    assert len(_CASES) == 10
    assert len({case["id"] for case in _CASES}) == len(_CASES)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
async def test_each_corpus_case_executes_the_real_runtime(case: dict[str, Any]) -> None:
    harness = _harness(case["scenario"])
    payload = await harness.handle(_TEXT)
    assert payload.status is TaskStatus(case["status"])
    assert harness.adapters["alertmanager"].call_count == case["alert_calls"]
    assert harness.adapters["prometheus"].call_count == case["metric_calls"]


async def test_repeat_is_stable_and_idempotent() -> None:
    harness = _harness("golden")
    first = await harness.handle(_TEXT)
    plan = (await harness.plan_store.load(task_id=harness.task_id)).plan
    plan_hash = compute_plan_hash(plan)
    call_hashes = tuple(compute_tool_call_hash(call) for call in harness.calls)

    second = await harness.handle(_TEXT)

    assert second == first
    assert harness.gateway.invocations == 2
    assert compute_plan_hash(
        (await harness.plan_store.load(task_id=harness.task_id)).plan
    ) == plan_hash
    assert tuple(compute_tool_call_hash(call) for call in harness.calls) == call_hashes


async def test_silenced_state_is_reported_without_claiming_resolution() -> None:
    harness = _harness("silenced")
    payload = await harness.handle(_TEXT)
    body = payload.model_dump_json()
    assert "silenced=True" in body
    assert "不是自动根因结论" in body


async def test_external_fields_are_not_present_in_the_result() -> None:
    harness = _harness("polluted")
    payload = await harness.handle(_TEXT)
    marker = "token" + "=" + "synthetic-value"
    assert marker not in payload.model_dump_json()
    assert "invalid.local" not in payload.model_dump_json()

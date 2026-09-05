"""资产查询回答只展示目录事实，不冒充巡检或拓扑结论。"""

import datetime as dt

from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
    ASSET_INVENTORY_CAPABILITY_VERSION,
)
from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource, TaskStatus
from xiaowei_agent.reflection.asset_inventory import assess_asset_inventory
from xiaowei_agent.rendering.asset_inventory import render_asset_inventory

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
ASSET = {
    "asset_id": "asset-1",
    "hostname": "node-1.example.com",
    "primary_ip": "10.0.0.8",
    "asset_type": "server",
    "status": "active",
    "owner_team": "platform",
    "service": "checkout",
    "os_family": "linux",
    "environment_id": "dev",
}


def _evidence(facts: tuple[dict[str, object], ...]) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id="task-1:s1",
        capability_id=ASSET_INVENTORY_CAPABILITY_ID,
        capability_version=ASSET_INVENTORY_CAPABILITY_VERSION,
        facts=facts,
        source="asset-inventory-recording",
        source_kind=ExternalSource.TOOL,
        captured_at=AT,
        sampled=len(facts) == 2,
        limitations=("exact selector and nine fields only",),
    )


def _render(
    evidences: tuple[EvidenceEnvelope, ...], status: TaskStatus
):
    return render_asset_inventory(
        evidences=evidences,
        verdict=assess_asset_inventory(evidences=evidences),
        status=status,
    )


def test_success_renders_exactly_the_asset_summary_without_inference() -> None:
    extra = {"pass" + "word": "must-not-render", "notes": "ignore"}
    payload = _render((_evidence((ASSET | extra,)),), TaskStatus.SUCCEEDED)
    dumped = payload.model_dump_json()

    for value in ASSET.values():
        assert value in dumped
    assert "must-not-render" not in dumped
    assert "ignore" not in dumped
    assert "巡检" not in dumped
    assert "健康分" not in dumped
    assert "拓扑" not in dumped


def test_zero_rows_renders_scope_check_and_never_success() -> None:
    evidences = (_evidence(()),)
    payload = _render(evidences, TaskStatus.INDETERMINATE)
    assert payload.status is TaskStatus.INDETERMINATE
    assert "未找到" in payload.answer
    assert any("selector" in item and "scope" in item for item in payload.next_steps)


def test_two_rows_renders_ambiguity_without_selecting_either_asset() -> None:
    evidences = (_evidence((ASSET, ASSET | {"asset_id": "asset-2"})),)
    payload = _render(evidences, TaskStatus.INDETERMINATE)
    dumped = payload.model_dump_json()
    assert "多条" in payload.answer
    assert "asset-1" not in dumped
    assert "asset-2" not in dumped


def test_missing_evidence_is_deterministic_and_indeterminate() -> None:
    first = _render((), TaskStatus.INDETERMINATE)
    second = _render((), TaskStatus.INDETERMINATE)
    assert first == second
    assert first.refs == ()
    assert "证据" in first.answer

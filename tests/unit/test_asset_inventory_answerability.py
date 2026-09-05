"""资产精确查询证据的确定性可答性真值表。"""

import datetime as dt

from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
    ASSET_INVENTORY_CAPABILITY_VERSION,
)
from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource, TaskStatus
from xiaowei_agent.reflection.asset_inventory import assess_asset_inventory
from xiaowei_agent.reflection.status import terminal_status_for

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


def _evidence(
    facts: tuple[dict[str, object], ...],
    *,
    capability_id: str = ASSET_INVENTORY_CAPABILITY_ID,
) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id="task-1:s1",
        capability_id=capability_id,
        capability_version=ASSET_INVENTORY_CAPABILITY_VERSION,
        facts=facts,
        source="asset-inventory-recording",
        source_kind=ExternalSource.TOOL,
        captured_at=AT,
        sampled=len(facts) == 2,
        limitations=("exact selector only",),
    )


def test_one_valid_asset_is_answerable() -> None:
    verdict = assess_asset_inventory(evidences=(_evidence((ASSET,)),))
    assert verdict.sufficient is True
    assert verdict.missing == ()
    assert terminal_status_for(verdict) is TaskStatus.SUCCEEDED


def test_zero_rows_is_indeterminate_without_inventing_an_asset() -> None:
    verdict = assess_asset_inventory(evidences=(_evidence(()),))
    assert verdict.sufficient is False
    assert verdict.needs_user_input is False
    assert any(item.reason_key == "evidence.empty" for item in verdict.missing)
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_two_rows_is_indeterminate_and_never_auto_selects() -> None:
    verdict = assess_asset_inventory(
        evidences=(_evidence((ASSET, ASSET | {"asset_id": "asset-2"})),)
    )
    assert verdict.sufficient is False
    assert verdict.needs_user_input is False
    assert any(item.reason_key == "evidence.ambiguous" for item in verdict.missing)
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_timeout_or_malformed_builder_failure_is_indeterminate_as_absent_evidence() -> None:
    verdict = assess_asset_inventory(evidences=())
    assert verdict.sufficient is False
    assert any(item.reason_key == "evidence.absent" for item in verdict.missing)
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE


def test_foreign_capability_evidence_is_not_answerable() -> None:
    verdict = assess_asset_inventory(
        evidences=(_evidence((ASSET,), capability_id="test.foreign"),)
    )
    assert verdict.sufficient is False
    assert verdict.downgrade_suggestion is True
    assert terminal_status_for(verdict) is TaskStatus.INDETERMINATE

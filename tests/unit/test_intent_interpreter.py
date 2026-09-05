"""M6a Prometheus 意图先判域，再只提取该域槽位。"""

import pytest

from xiaowei_agent.capabilities.intent import (
    ASSET_INVENTORY_INTENT,
    PROMETHEUS_ALERT_INTENT,
    RuleBasedIntentInterpreter,
)
from xiaowei_agent.contracts import RequestContext

CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "查告警 HostHighCpu 在 node-1.example.com:9100 最近30分钟的证据",
            {
                "alert_name": "HostHighCpu",
                "instance": "node-1.example.com:9100",
                "window_minutes": "30",
            },
        ),
        (
            "告警名=InstanceDown instance=10.0.0.8:9100 最近1小时证据",
            {
                "alert_name": "InstanceDown",
                "instance": "10.0.0.8:9100",
                "window_minutes": "60",
            },
        ),
        (
            "告警名=HostHighCpu instance=node-1:9100 fingerprint=fp-001 查证据",
            {
                "alert_name": "HostHighCpu",
                "instance": "node-1:9100",
                "fingerprint": "fp-001",
            },
        ),
    ],
)
def test_prometheus_alert_phrasings_have_exact_slots(
    text: str, expected: dict[str, str]
) -> None:
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)
    assert draft.intent == PROMETHEUS_ALERT_INTENT
    assert dict(draft.slots) == expected
    assert draft.missing == ()


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        ("查告警证据", ("alert_name", "instance")),
        ("查告警 HostHighCpu 的证据", ("instance",)),
        ("查 node-1:9100 的告警证据", ("alert_name",)),
    ],
)
def test_missing_prometheus_slots_are_declared_not_invented(
    text: str, missing: tuple[str, ...]
) -> None:
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)
    assert draft.intent == PROMETHEUS_ALERT_INTENT
    assert draft.missing == missing


def test_ambiguous_cross_domain_text_does_not_choose_a_capability() -> None:
    draft = RuleBasedIntentInterpreter().interpret(
        text="查慢查询和告警 HostHighCpu 在 node-1:9100 的证据",
        context=CONTEXT,
    )
    assert draft.intent == "unknown"
    assert draft.slots == {}


def test_prometheus_slot_allowlist_is_domain_local() -> None:
    assert RuleBasedIntentInterpreter.SLOT_ALLOWLISTS[PROMETHEUS_ALERT_INTENT] == (
        frozenset({"alert_name", "instance", "fingerprint", "window_minutes"})
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("查资产 asset_id=Asset-01", {"asset_id": "Asset-01"}),
        (
            "查询资产 hostname=NODE-1.EXAMPLE.COM.",
            {"hostname": "NODE-1.EXAMPLE.COM."},
        ),
        ("查机器 ip=10.0.0.8", {"ip": "10.0.0.8"}),
        ("查询资产 ip=2001:db8::1", {"ip": "2001:db8::1"}),
    ],
)
def test_asset_lookup_phrasings_emit_only_the_exact_selector(
    text: str, expected: dict[str, str]
) -> None:
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT)
    assert draft.intent == ASSET_INVENTORY_INTENT
    assert dict(draft.slots) == expected
    assert draft.missing == ()


def test_asset_lookup_without_a_selector_declares_the_missing_slot() -> None:
    draft = RuleBasedIntentInterpreter().interpret(text="查询资产", context=CONTEXT)
    assert draft.intent == ASSET_INVENTORY_INTENT
    assert draft.slots == {}
    assert draft.missing == ("asset_selector",)


@pytest.mark.parametrize(
    "text",
    [
        "列出全部资产",
        "模糊找机器",
        "查询 10.0.0.0/24 网段资产",
        "资产巡检",
        "修改资产 asset_id=asset-1",
        "查告警 HostHighCpu 在 node-1:9100 的证据",
    ],
)
def test_asset_near_misses_do_not_claim_the_lookup_intent(text: str) -> None:
    assert (
        RuleBasedIntentInterpreter().interpret(text=text, context=CONTEXT).intent
        != ASSET_INVENTORY_INTENT
    )


def test_multiple_asset_selectors_are_not_silently_reduced_to_one() -> None:
    draft = RuleBasedIntentInterpreter().interpret(
        text="查询资产 asset_id=asset-1 hostname=node-1.example.com",
        context=CONTEXT,
    )
    assert draft.intent == ASSET_INVENTORY_INTENT
    assert dict(draft.slots) == {
        "asset_id": "asset-1",
        "hostname": "node-1.example.com",
    }

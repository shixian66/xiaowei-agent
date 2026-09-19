"""资产精确查询参数只允许一个已规范化 selector。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.application.capability_input import (
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
)
from xiaowei_agent.application.default_capabilities import ASSET_INVENTORY_BINDING
from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
    ASSET_INVENTORY_CAPABILITY_VERSION,
    ASSET_INVENTORY_INPUT_SCHEMA_REF,
    OP_LOOKUP_ASSET,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    CapabilitySubject,
    ClarificationContext,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedSlot,
    IntentDraft,
    IntentSource,
    RequestContext,
)
from xiaowei_agent.planning.assets.params import AssetLookupParams
from xiaowei_agent.planning.slot_verification import (
    confirmed_text_value,
    require_confirmed_projection,
)

_SNAPSHOT = StaticCapabilityRegistry().snapshot()
_CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-05.2",
)


def _draft(
    *,
    source: IntentSource = IntentSource.USER,
    missing: tuple[str, ...] = (),
    **slots: str,
) -> IntentDraft:
    return IntentDraft(
        intent=ASSET_INVENTORY_CAPABILITY_ID,
        slots=slots,
        missing=missing,
        confidence=0.9,
        source=source,
    )


def _candidate() -> object:
    return next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=_draft(asset_id="asset-1"), context=_CONTEXT, snapshot=_SNAPSHOT)
        .items
        if item.operation == OP_LOOKUP_ASSET
    )


def _verify(
    draft: IntentDraft,
    *,
    clarification: ClarificationContext | None = None,
    user_text: str | None = None,
) -> SlotReady[AssetLookupParams] | SlotIncomplete | SlotInvalid:
    result = ASSET_INVENTORY_BINDING.input_binding.slot_verifier(
        candidate=_candidate(),
        draft=draft,
        context=_CONTEXT,
        as_of=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
        user_text=user_text or _user_text_from_draft(draft),
        clarification=clarification,
    )
    assert isinstance(result, SlotReady | SlotIncomplete | SlotInvalid)
    return result


def _ready(
    draft: IntentDraft,
    *,
    clarification: ClarificationContext | None = None,
    user_text: str | None = None,
) -> SlotReady[AssetLookupParams]:
    result = _verify(draft, clarification=clarification, user_text=user_text)
    assert isinstance(result, SlotReady)
    return result


def _user_text_from_draft(draft: IntentDraft) -> str:
    if draft.source is not IntentSource.USER:
        return "查资产"
    if asset_id := draft.slots.get("asset_id"):
        return f"查资产 asset_id={asset_id}"
    if hostname := draft.slots.get("hostname"):
        return f"查资产 hostname={hostname}"
    if ip := draft.slots.get("ip"):
        return f"查资产 ip={ip}"
    return "查资产"


def _slot(field: ClarificationField, value: object) -> ConfirmedSlot:
    return ConfirmedSlot(field=field, value=value)


def _snapshot(*slots: ConfirmedSlot) -> tuple[ConfirmedSlot, ...]:
    return tuple(sorted(slots, key=lambda slot: slot.field.value))


def _clarification(*slots: ConfirmedSlot) -> ClarificationContext:
    confirmed = _snapshot(*slots)
    subject = CapabilitySubject(
        kind="capability",
        capability_id=ASSET_INVENTORY_CAPABILITY_ID,
        capability_version=ASSET_INVENTORY_CAPABILITY_VERSION,
        operation=OP_LOOKUP_ASSET,
        input_schema_ref=ASSET_INVENTORY_INPUT_SCHEMA_REF,
        confirmed_slots=confirmed,
    )
    return ClarificationContext(subject=subject, confirmed_slots=confirmed)


def test_asset_verifier_returns_selector_required_for_no_selector() -> None:
    result = _verify(_draft(missing=("asset_selector",)))

    assert isinstance(result, SlotIncomplete)
    assert result.reason_code is ClarificationReasonCode.CAPABILITY_ASSET_SELECTOR_REQUIRED
    assert result.missing_fields == (
        ClarificationField.ASSET_ID,
        ClarificationField.HOSTNAME,
        ClarificationField.IP,
    )
    assert result.confirmed_slots == ()


def test_asset_verifier_does_not_trust_model_supplied_selector() -> None:
    result = _verify(_draft(source=IntentSource.MODEL, asset_id="asset-1"))

    assert isinstance(result, SlotIncomplete)
    assert result.confirmed_slots == ()


def test_asset_verifier_promotes_and_canonicalizes_one_user_selector() -> None:
    ready = _ready(_draft(hostname="NODE-1.EXAMPLE.COM."))

    assert ready.params.selector() == ("hostname", "node-1.example.com")
    assert ready.confirmed_slots == (
        _slot(ClarificationField.HOSTNAME, confirmed_text_value("node-1.example.com")),
    )


def test_asset_verifier_rejects_multiple_current_selectors() -> None:
    result = _verify(
        _draft(),
        user_text="查资产 asset_id=asset-1 hostname=node-1.example.com",
    )

    assert isinstance(result, SlotInvalid)


def test_asset_verifier_inherits_parent_selector() -> None:
    parent = _clarification(
        _slot(ClarificationField.ASSET_ID, confirmed_text_value("asset-1"))
    )

    ready = _ready(_draft(), clarification=parent)

    assert ready.params.selector() == ("asset_id", "asset-1")
    assert ready.confirmed_slots == parent.confirmed_slots


def test_asset_projector_covers_confirmed_selector_from_plan_and_target() -> None:
    ready = _ready(_draft(hostname="NODE-1.EXAMPLE.COM."))
    prepared = ASSET_INVENTORY_BINDING.input_binding.planner(
        candidate=_candidate(),
        params=ready.params,
        context=_CONTEXT,
        snapshot=_SNAPSHOT,
    )
    projector = ASSET_INVENTORY_BINDING.input_binding.confirmed_slot_projector
    assert projector is not None

    projected = projector(plan=prepared.plan, target=prepared.target)

    assert require_confirmed_projection(
        confirmed=ready.confirmed_slots, projected=projected
    ) == projected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"asset_id": "Cafe\u0301-01"}, ("asset_id", "Café-01")),
        ({"hostname": "NODE-1.EXAMPLE.COM."}, ("hostname", "node-1.example.com")),
        ({"ip": "10.0.0.8"}, ("ip", "10.0.0.8")),
        ({"ip": "2001:0DB8:0:0::1"}, ("ip", "2001:db8::1")),
    ],
)
def test_each_exact_selector_has_one_canonical_form(
    payload: dict[str, str], expected: tuple[str, str]
) -> None:
    assert AssetLookupParams(**payload).selector() == expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"asset_id": "asset-1", "hostname": "node-1.example.com"},
        {"asset_id": "asset-1", "ip": "10.0.0.8"},
        {"hostname": "node-1.example.com", "ip": "10.0.0.8"},
        {
            "asset_id": "asset-1",
            "hostname": "node-1.example.com",
            "ip": "10.0.0.8",
        },
    ],
)
def test_exactly_one_selector_is_required(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        AssetLookupParams(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_id", "asset id"),
        ("asset_id", "asset/1"),
        ("asset_id", "*"),
        ("asset_id", "a" * 129),
        ("hostname", "node-*.example.com"),
        ("hostname", "node_1.example.com"),
        ("hostname", "-node.example.com"),
        ("hostname", "node..example.com"),
        ("ip", "10.0.0.0/24"),
        ("ip", "10.0.0.1-10.0.0.8"),
        ("ip", "fe80::1%eth0"),
        ("ip", "10.0.0.8:9100"),
        ("ip", "[2001:db8::1]:9100"),
    ],
)
def test_fuzzy_or_ambiguous_selectors_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        AssetLookupParams(**{field: value})


def test_asset_id_is_case_sensitive() -> None:
    assert AssetLookupParams(asset_id="Asset-1").selector() != (
        "asset_id",
        "asset-1",
    )


@pytest.mark.parametrize("field", ["gateway", "operation", "tenant_id", "environment_id"])
def test_execution_and_scope_fields_are_not_parameters(field: str) -> None:
    with pytest.raises(ValidationError):
        AssetLookupParams(asset_id="asset-1", **{field: "polluted"})

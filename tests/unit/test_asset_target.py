"""资产 selector 与 RequestContext 共同形成稳定目标指纹。"""

import pytest

from xiaowei_agent.contracts import RequestContext
from xiaowei_agent.planning import compute_target_fingerprint
from xiaowei_agent.planning.assets.compiler import resolve_asset_target
from xiaowei_agent.planning.assets.params import AssetLookupParams


def _context(**overrides: str) -> RequestContext:
    values = {
        "tenant_id": "dev-local",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-test",
    }
    return RequestContext(**(values | overrides))


def test_target_contains_only_context_scope_and_canonical_selector() -> None:
    target = resolve_asset_target(
        context=_context(),
        params=AssetLookupParams(hostname="NODE-1.EXAMPLE.COM."),
    )
    assert target.model_dump() == {
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "provider": "asset_inventory",
        "resource_kind": "asset",
        "resource_ids": ("hostname:node-1.example.com",),
        "selector_version": "asset.exact.v1",
    }


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            AssetLookupParams(asset_id="Cafe\u0301-01"),
            AssetLookupParams(asset_id="Café-01"),
        ),
        (
            AssetLookupParams(hostname="NODE-1.EXAMPLE.COM."),
            AssetLookupParams(hostname="node-1.example.com"),
        ),
        (
            AssetLookupParams(ip="2001:0DB8:0:0::1"),
            AssetLookupParams(ip="2001:db8::1"),
        ),
    ],
)
def test_equivalent_selector_spellings_have_the_same_fingerprint(
    first: AssetLookupParams, second: AssetLookupParams
) -> None:
    assert compute_target_fingerprint(
        resolve_asset_target(context=_context(), params=first)
    ) == compute_target_fingerprint(
        resolve_asset_target(context=_context(), params=second)
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (AssetLookupParams(asset_id="asset-1"), AssetLookupParams(asset_id="asset-2")),
        (
            AssetLookupParams(asset_id="asset-1"),
            AssetLookupParams(hostname="asset-1"),
        ),
    ],
)
def test_selector_kind_or_value_changes_the_fingerprint(
    first: AssetLookupParams, second: AssetLookupParams
) -> None:
    assert compute_target_fingerprint(
        resolve_asset_target(context=_context(), params=first)
    ) != compute_target_fingerprint(
        resolve_asset_target(context=_context(), params=second)
    )


@pytest.mark.parametrize(
    "context",
    [
        _context(tenant_id="other-tenant"),
        _context(environment_id="test"),
    ],
)
def test_context_scope_changes_the_fingerprint(context: RequestContext) -> None:
    params = AssetLookupParams(asset_id="asset-1")
    baseline = resolve_asset_target(context=_context(), params=params)
    changed = resolve_asset_target(context=context, params=params)
    assert compute_target_fingerprint(baseline) != compute_target_fingerprint(changed)


def test_actor_is_not_part_of_the_target_fingerprint() -> None:
    params = AssetLookupParams(asset_id="asset-1")
    alice = resolve_asset_target(context=_context(actor="alice"), params=params)
    bob = resolve_asset_target(context=_context(actor="bob"), params=params)
    assert compute_target_fingerprint(alice) == compute_target_fingerprint(bob)


def test_unregistered_environment_fails_closed() -> None:
    with pytest.raises(ValueError, match="environment"):
        resolve_asset_target(
            context=_context(environment_id="prod"),
            params=AssetLookupParams(asset_id="asset-1"),
        )

"""资产精确查询参数只允许一个已规范化 selector。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.planning.assets.params import AssetLookupParams


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

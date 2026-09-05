"""资产 fake 不得跨 tenant/environment 回退或泄漏存在性。"""

import pytest

from xiaowei_agent.contracts import AdapterStatus, RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.asset_inventory_fake import (
    AssetInventoryRecordingAdapter,
    AssetInventoryRecordingNotFoundError,
)

pytestmark = pytest.mark.security

_ROW = {
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


def _context(*, tenant_id: str = "dev-local", environment_id: str = "dev") -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        actor="alice",
        environment_id=environment_id,
        trace_id="0" * 32,
        policy_revision="policy-test",
    )


def _call() -> ToolCall:
    return ToolCall(
        gateway="asset_inventory",
        operation="lookup_asset",
        step_id="s1",
        typed_args={
            "selector_kind": "hostname",
            "selector_value": "node-1.example.com",
            "field_set_id": "asset.summary.v1",
            "limit": 2,
        },
        timeout_seconds=30.0,
        idempotency_key="fixed:s1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _context(tenant_id="other-tenant"),
        _context(environment_id="test"),
    ],
)
async def test_same_selector_in_another_scope_never_falls_back(
    context: RequestContext,
) -> None:
    response = AdapterResponse(
        status=AdapterStatus.OK,
        payload=(_ROW,),
        source="asset-inventory-recording",
        error=None,
        elapsed_ms=1,
    )
    adapter = AssetInventoryRecordingAdapter(
        {("dev-local", "dev", "hostname", "node-1.example.com"): response},
        operation="lookup_asset",
    )
    with pytest.raises(AssetInventoryRecordingNotFoundError) as caught:
        await adapter.execute(_call(), context=context)
    assert str(caught.value) == "no recording for this call"
    assert adapter.call_count == 1

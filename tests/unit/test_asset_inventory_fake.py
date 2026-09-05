"""资产 recording adapter 只接受精确 operation、scope 与参数闭集。"""

import pytest

from xiaowei_agent.contracts import (
    AdapterStatus,
    RequestContext,
    ToolCall,
)
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.asset_inventory_fake import (
    AssetInventoryRecordingAdapter,
    AssetInventoryRecordingNotFoundError,
)
from xiaowei_agent.tools.asset_inventory_recording import (
    default_asset_inventory_recording,
)

OPERATION = "lookup_asset"
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-test",
)


def _call(**overrides: object) -> ToolCall:
    arguments: dict[str, object] = {
        "selector_kind": "asset_id",
        "selector_value": "asset-1",
        "field_set_id": "asset.summary.v1",
        "limit": 2,
    }
    arguments.update(overrides.pop("typed_args", {}))
    values = {
        "gateway": "asset_inventory",
        "operation": OPERATION,
        "step_id": "s1",
        "typed_args": arguments,
        "timeout_seconds": 30.0,
        "idempotency_key": "fixed:s1",
    }
    return ToolCall(**(values | overrides))


@pytest.mark.asyncio
async def test_exact_recording_is_returned_and_call_is_recorded() -> None:
    adapter = AssetInventoryRecordingAdapter(
        default_asset_inventory_recording(), operation=OPERATION
    )
    call = _call()
    response = await adapter.execute(call, context=CONTEXT)
    assert response.status is AdapterStatus.OK
    assert len(response.payload) == 1
    assert response.payload[0]["asset_id"] == "asset-1"
    assert adapter.call_count == 1
    assert adapter.calls == [call]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        _call(operation="unknown"),
        _call(typed_args={"limit": 1}),
        _call(typed_args={"field_set_id": "asset.full.v1"}),
        _call(typed_args={"gateway": "evil"}),
        _call(typed_args={"selector_kind": "unknown"}),
    ],
)
async def test_unknown_operation_or_argument_shape_has_no_fallback(call: ToolCall) -> None:
    adapter = AssetInventoryRecordingAdapter(
        default_asset_inventory_recording(), operation=OPERATION
    )
    with pytest.raises(AssetInventoryRecordingNotFoundError):
        await adapter.execute(call, context=CONTEXT)
    assert adapter.call_count == 1


@pytest.mark.asyncio
async def test_explicit_empty_recording_result_is_distinct_from_missing_key() -> None:
    empty = AdapterResponse(
        status=AdapterStatus.OK,
        payload=(),
        source="asset-inventory-recording",
        error=None,
        elapsed_ms=1,
    )
    adapter = AssetInventoryRecordingAdapter(
        {("dev-local", "dev", "asset_id", "missing"): empty},
        operation=OPERATION,
    )
    response = await adapter.execute(
        _call(typed_args={"selector_value": "missing"}), context=CONTEXT
    )
    assert response.status is AdapterStatus.OK
    assert response.payload == ()
    with pytest.raises(AssetInventoryRecordingNotFoundError):
        await adapter.execute(_call(), context=CONTEXT)


def test_packaged_recording_uses_only_the_nine_scalar_evidence_fields() -> None:
    allowed = {
        "asset_id",
        "hostname",
        "primary_ip",
        "asset_type",
        "status",
        "owner_team",
        "service",
        "os_family",
        "environment_id",
    }
    recording = default_asset_inventory_recording()
    assert recording
    for response in recording.values():
        assert response.status is AdapterStatus.OK
        for row in response.payload:
            assert set(row) == allowed
            assert all(not isinstance(value, dict | list) for value in row.values())


def test_empty_recording_is_rejected() -> None:
    with pytest.raises(ValueError):
        AssetInventoryRecordingAdapter({}, operation=OPERATION)

"""M6a 内置资产 synthetic recording；不触达外部系统。"""

from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.contracts import AdapterStatus
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingKey

IS_FAKE: Final[bool] = True
_SOURCE: Final[str] = "asset-inventory-recording"


def _response(
    *,
    asset_id: str,
    hostname: str,
    primary_ip: str,
    service: str,
) -> AdapterResponse:
    row: Mapping[str, Any] = {
        "asset_id": asset_id,
        "hostname": hostname,
        "primary_ip": primary_ip,
        "asset_type": "server",
        "status": "active",
        "owner_team": "platform",
        "service": service,
        "os_family": "linux",
        "environment_id": "dev",
    }
    return AdapterResponse(
        status=AdapterStatus.OK,
        payload=(row,),
        source=_SOURCE,
        error=None,
        elapsed_ms=1,
    )


def default_asset_inventory_recording(
) -> Mapping[AssetInventoryRecordingKey, AdapterResponse]:
    """返回一项可由三类 selector 精确命中的固定资产。"""
    asset = _response(
        asset_id="asset-1",
        hostname="node-1.example.com",
        primary_ip="10.0.0.8",
        service="checkout",
    )
    return {
        ("dev-local", "dev", "asset_id", "asset-1"): asset,
        ("dev-local", "dev", "hostname", "node-1.example.com"): asset,
        ("dev-local", "dev", "ip", "10.0.0.8"): asset,
    }

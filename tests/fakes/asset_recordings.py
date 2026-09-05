"""资产查询 Runtime 契约使用的合成 recordings。"""

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.contracts import AdapterStatus, ExternalContent, ExternalSource
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingKey

_HOSTNAME_KEY: Final[AssetInventoryRecordingKey] = (
    "dev-local",
    "dev",
    "hostname",
    "node-1.example.com",
)
_ASSET_ID_KEY: Final[AssetInventoryRecordingKey] = (
    "dev-local",
    "dev",
    "asset_id",
    "asset-1",
)
_IP_KEY: Final[AssetInventoryRecordingKey] = (
    "dev-local",
    "dev",
    "ip",
    "10.0.0.8",
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
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
    return row | overrides


def _ok(*rows: Mapping[str, object]) -> AdapterResponse:
    return AdapterResponse(
        status=AdapterStatus.OK,
        payload=tuple(rows),
        source="asset-inventory-recording",
        error=None,
        elapsed_ms=1,
    )


def recording_for(
    scenario: str,
) -> Mapping[AssetInventoryRecordingKey, Any]:
    """返回指定资产 Runtime 情形的精确 recording。"""
    response: Any
    key = _HOSTNAME_KEY
    if scenario in {"golden", "hostname"}:
        response = _ok(_row())
    elif scenario == "asset_id":
        key = _ASSET_ID_KEY
        response = _ok(_row())
    elif scenario == "ip":
        key = _IP_KEY
        response = _ok(_row())
    elif scenario == "empty":
        response = _ok()
    elif scenario == "duplicate":
        response = _ok(_row(), _row(asset_id="asset-2"))
    elif scenario == "timeout":
        response = AdapterResponse(
            status=AdapterStatus.TIMEOUT,
            payload=(),
            source="asset-inventory-recording",
            error=None,
            elapsed_ms=30_000,
        )
    elif scenario == "malformed":
        response = _ok(_row(status=1))
    elif scenario == "wrong_environment":
        response = _ok(_row(environment_id="test"))
    elif scenario == "wrong_identity":
        response = _ok(_row(hostname="node-2.example.com"))
    elif scenario == "polluted":
        response = _ok(
            _row(
                credential="to" + "ken" + "=" + "synthetic-value",
                external_instruction="must-not-render",
            )
        )
    elif scenario == "cross_scope":
        marker = "pass" + "word" + "=" + "synthetic-upstream"
        return {
            (
                "other-tenant",
                "prod",
                "hostname",
                "node-1.example.com",
            ): AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=(),
                source="asset-inventory-recording",
                error=ExternalContent.capture(
                    source=ExternalSource.TOOL,
                    content=marker,
                    captured_at=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
                ),
                elapsed_ms=2,
            )
        }
    else:
        raise ValueError("unknown asset inventory recording scenario")
    return {key: response}

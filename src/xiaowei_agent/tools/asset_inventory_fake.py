"""按完整 scope 与精确 selector 回放的资产目录 fake。"""

from collections.abc import Mapping
from typing import Final

from xiaowei_agent.contracts import RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True

AssetInventoryRecordingKey = tuple[str, str, str, str]
_ARGUMENT_KEYS: Final[frozenset[str]] = frozenset(
    {"selector_kind", "selector_value", "field_set_id", "limit"}
)
_SELECTOR_KINDS: Final[frozenset[str]] = frozenset(
    {"asset_id", "hostname", "ip"}
)


class AssetInventoryRecordingNotFoundError(LookupError):
    """调用不在精确 recording 闭集中。"""


def _recording_key(
    call: ToolCall, context: RequestContext, *, operation: str
) -> AssetInventoryRecordingKey | None:
    arguments = call.typed_args
    if call.operation != operation or set(arguments) != _ARGUMENT_KEYS:
        return None
    selector_kind = arguments.get("selector_kind")
    selector_value = arguments.get("selector_value")
    field_set_id = arguments.get("field_set_id")
    limit = arguments.get("limit")
    if (
        not isinstance(selector_kind, str)
        or selector_kind not in _SELECTOR_KINDS
        or not isinstance(selector_value, str)
    ):
        return None
    if field_set_id != "asset.summary.v1" or type(limit) is not int or limit != 2:
        return None
    return (
        context.tenant_id,
        context.environment_id,
        selector_kind,
        selector_value,
    )


class AssetInventoryRecordingAdapter:
    """无 scope 或参数回退的资产内存 recording adapter。"""

    def __init__(
        self,
        recording: Mapping[AssetInventoryRecordingKey, AdapterResponse],
        *,
        operation: str,
    ) -> None:
        if not recording:
            raise ValueError("AssetInventoryRecordingAdapter requires at least one entry")
        if not operation:
            raise ValueError("asset inventory operation is required")
        self._recording = dict(recording)
        self._operation = operation
        self.call_count = 0
        self.calls: list[ToolCall] = []

    async def execute(
        self, call: ToolCall, *, context: RequestContext
    ) -> AdapterResponse:
        self.calls.append(call)
        self.call_count += 1
        key = _recording_key(call, context, operation=self._operation)
        if key is None:
            raise AssetInventoryRecordingNotFoundError(
                "no recording for this call"
            )
        try:
            return self._recording[key]
        except KeyError:
            raise AssetInventoryRecordingNotFoundError(
                "no recording for this call"
            ) from None

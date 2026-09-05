"""按完整 scope 与参数精确回放的 Alertmanager fake。"""

from collections.abc import Mapping
from typing import Final

from xiaowei_agent.contracts import RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True

AlertmanagerRecordingKey = tuple[str, str, str, str, str, str | None, int]
_BASE_KEYS: Final[frozenset[str]] = frozenset({"alert_name", "instance", "limit"})


class AlertmanagerRecordingNotFoundError(LookupError):
    """调用不在精确 recording 闭集中。"""


def _recording_key(
    call: ToolCall, context: RequestContext
) -> AlertmanagerRecordingKey | None:
    arguments = call.typed_args
    keys = set(arguments)
    if keys not in {_BASE_KEYS, _BASE_KEYS | {"fingerprint"}}:
        return None
    alert_name = arguments.get("alert_name")
    instance = arguments.get("instance")
    fingerprint = arguments.get("fingerprint")
    limit = arguments.get("limit")
    if not isinstance(alert_name, str) or not isinstance(instance, str):
        return None
    if fingerprint is not None and not isinstance(fingerprint, str):
        return None
    if type(limit) is not int or limit != 5:
        return None
    return (
        context.tenant_id,
        context.environment_id,
        call.operation,
        alert_name,
        instance,
        fingerprint,
        limit,
    )


class AlertmanagerRecordingAdapter:
    """无默认回退的 Alertmanager 内存 recording adapter。"""

    def __init__(
        self, recording: Mapping[AlertmanagerRecordingKey, AdapterResponse]
    ) -> None:
        if not recording:
            raise ValueError("AlertmanagerRecordingAdapter requires at least one entry")
        self._recording = dict(recording)
        self.call_count = 0
        self.calls: list[ToolCall] = []

    async def execute(
        self, call: ToolCall, *, context: RequestContext
    ) -> AdapterResponse:
        self.calls.append(call)
        self.call_count += 1
        key = _recording_key(call, context)
        if key is None:
            raise AlertmanagerRecordingNotFoundError(
                "no recording for this call"
            )
        try:
            return self._recording[key]
        except KeyError:
            raise AlertmanagerRecordingNotFoundError(
                "no recording for this call"
            ) from None

"""按完整 query_range 调用精确回放的 Prometheus fake。"""

from collections.abc import Mapping
from typing import Final

from xiaowei_agent.contracts import RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True

PrometheusRecordingKey = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    int,
    int,
]
_ARGUMENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "promql",
        "promql_template_id",
        "alert_name",
        "instance",
        "window_start",
        "window_end",
        "step_seconds",
        "max_series",
        "max_points_per_series",
    }
)


class PrometheusRecordingNotFoundError(LookupError):
    """调用不在精确 recording 闭集中。"""


def _recording_key(
    call: ToolCall, context: RequestContext
) -> PrometheusRecordingKey | None:
    arguments = call.typed_args
    if set(arguments) != _ARGUMENT_KEYS:
        return None
    promql = arguments.get("promql")
    template_id = arguments.get("promql_template_id")
    alert_name = arguments.get("alert_name")
    instance = arguments.get("instance")
    start = arguments.get("window_start")
    end = arguments.get("window_end")
    if not isinstance(promql, str):
        return None
    if not isinstance(template_id, str):
        return None
    if not isinstance(alert_name, str):
        return None
    if not isinstance(instance, str):
        return None
    if not isinstance(start, str):
        return None
    if not isinstance(end, str):
        return None
    step_seconds = arguments.get("step_seconds")
    max_series = arguments.get("max_series")
    max_points = arguments.get("max_points_per_series")
    if (
        type(step_seconds) is not int
        or type(max_series) is not int
        or type(max_points) is not int
        or step_seconds != 60
        or max_series != 5
        or max_points != 361
    ):
        return None
    return (
        context.tenant_id,
        context.environment_id,
        call.operation,
        promql,
        template_id,
        alert_name,
        instance,
        start,
        end,
        step_seconds,
        max_series,
        max_points,
    )


class PrometheusRecordingAdapter:
    """无默认回退的 Prometheus 内存 recording adapter。"""

    def __init__(
        self, recording: Mapping[PrometheusRecordingKey, AdapterResponse]
    ) -> None:
        if not recording:
            raise ValueError("PrometheusRecordingAdapter requires at least one entry")
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
            raise PrometheusRecordingNotFoundError("no recording for this call")
        try:
            return self._recording[key]
        except KeyError:
            raise PrometheusRecordingNotFoundError(
                "no recording for this call"
            ) from None

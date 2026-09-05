"""镜像内置的最小合成 StarRocks recording；不触达外部系统。"""

from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.contracts import AdapterStatus
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True
_SOURCE: Final[str] = "starrocks-recording"


def default_recording(
    *, list_operation: str, count_operation: str
) -> Mapping[tuple[str, str | None], AdapterResponse]:
    """由组装根传入 capability operation，避免 tools 反向依赖 capabilities。"""
    row: Mapping[str, Any] = {
        "queryId": "synthetic-query-1",
        "timestamp": "2026-09-05 11:55:00",
        "queryTime": 12_000,
        "scanRows": 1_000,
        "returnRows": 10,
        "scanBytes": 2_048,
        "memCostBytes": 4_096,
        "pendingTimeMs": 5,
        "cpuCostNs": 1_000_000,
        "state": "FINISHED",
        "errorCode": "",
        "db": "synthetic_db",
        "user": "synthetic_user",
    }
    return {
        (list_operation, None): AdapterResponse(
            status=AdapterStatus.OK,
            payload=(row,),
            source=_SOURCE,
            error=None,
            elapsed_ms=1,
        ),
        (count_operation, None): AdapterResponse(
            status=AdapterStatus.OK,
            payload=({"query_count": 1},),
            source=_SOURCE,
            error=None,
            elapsed_ms=1,
        ),
    }

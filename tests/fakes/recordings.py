"""脱敏的 StarRocks 审计行 recording。

**全部行都是构造的**：不含真实生产数据、主机名、IP、用户名或 SQL 原文；``user``
一律是 ``app_user_1`` 之类的合成值；不出现 ``stmt`` 列。
"""

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST
from xiaowei_agent.contracts import AdapterStatus
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.starrocks_fake import RecordingKey

_SOURCE: Final[str] = "starrocks-fake"


def _row(index: int, *, query_time: int, database: str = "sales") -> dict[str, Any]:
    at = dt.datetime(2026, 9, 2, 11, 40, tzinfo=dt.UTC) + dt.timedelta(minutes=index)
    return {
        "queryId": f"q-{index}",
        "timestamp": at.strftime("%Y-%m-%d %H:%M:%S"),
        "queryTime": query_time,
        "scanRows": 1_000 * (index + 1),
        "returnRows": 10,
        "scanBytes": 2_048 * (index + 1),
        "memCostBytes": 4_096,
        "pendingTimeMs": 5,
        "cpuCostNs": 1_000_000,
        "state": "FINISHED",
        "errorCode": "",
        "db": database,
        "user": "app_user_1",
    }


def _ok(payload: tuple[Mapping[str, Any], ...]) -> AdapterResponse:
    return AdapterResponse(
        status=AdapterStatus.OK, payload=payload, source=_SOURCE, error=None, elapsed_ms=7
    )


def _count(value: int) -> AdapterResponse:
    return _ok(({"query_count": value},))


_SLOW_ROWS: Final[tuple[Mapping[str, Any], ...]] = (
    _row(0, query_time=32_000),
    _row(1, query_time=21_500),
    _row(2, query_time=11_200),
)

GOLDEN: Final[Mapping[RecordingKey, AdapterResponse]] = {
    (OP_LIST, None): _ok(_SLOW_ROWS),
    (OP_COUNT, None): _count(42),
}
"""s1 取到 3 行慢查询；s2 因条件不成立而不会被执行。"""

EMPTY_WITH_TRAFFIC: Final[Mapping[RecordingKey, AdapterResponse]] = {
    (OP_LIST, None): _ok(()),
    (OP_COUNT, None): _count(42),
}
"""范围内有查询但无慢查询——这是一个**确定的答案**，不是失败。"""

EMPTY_WITHOUT_TRAFFIC: Final[Mapping[RecordingKey, AdapterResponse]] = {
    (OP_LIST, None): _ok(()),
    (OP_COUNT, None): _count(0),
}
"""范围内没有任何审计数据——不能宣称"没有慢查询"，必须降级。"""

EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE: Final[Mapping[RecordingKey, AdapterResponse]] = {
    (OP_LIST, "sales"): _ok(()),
    (OP_COUNT, "sales"): _count(0),
    # 不带过滤时窗口内有大量流量。**这正是 B1 的场景**：若 count 模板不复用 s1 的
    # 目标过滤，它会命中下面这条 42，从而把"拿不到 sales 的审计数据"自信地
    # 报成"sales 没有慢查询"。
    (OP_LIST, None): _ok(()),
    (OP_COUNT, None): _count(42),
}

TIMEOUT: Final[Mapping[RecordingKey, AdapterResponse]] = {
    (OP_LIST, None): AdapterResponse(
        status=AdapterStatus.TIMEOUT, payload=(), source=_SOURCE, error=None, elapsed_ms=30_000
    ),
    (OP_COUNT, None): _count(0),
}


class _MalformedDuckType:
    """返回**非 AdapterResponse** 的鸭子对象。

    Protocol 只在静态检查时生效；运行时 Gateway 必须自己 fail-closed，否则未经契约
    约束的数据会一路带进 ToolResult。
    """

    status = AdapterStatus.OK
    payload: tuple[Mapping[str, Any], ...] = ({"queryId": "injected"},)
    source = _SOURCE
    error = None
    elapsed_ms = 1


MALFORMED: Final[Mapping[RecordingKey, Any]] = {
    (OP_LIST, None): _MalformedDuckType(),
    (OP_COUNT, None): _count(0),
}

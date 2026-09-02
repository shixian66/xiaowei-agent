"""按调用内容回放的 StarRocks fake adapter。**不触达任何被管运维目标。**

与 M2 的 ``RecordingToolAdapter`` 的差别只有一条：后者按**调用序号**回放，无法表达
"s1 空而 s2 有行"这种**依赖调用内容**的场景，而可选只读分支的两个分支恰恰要靠它
区分。因此本 adapter 按调用内容索引；序号回放仍由 M2 的实现承担。

索引键是 ``(operation, database 过滤)``——这是区分 B1 回归语料所需的**最小**内容
依赖：同一窗口下，"按范围过滤后无数据"与"不过滤则有大量流量"必须能回放成两个
不同的结果，否则那条回归用例根本无法表达。

``call_count`` / ``calls`` 使"adapter 调用次数为 0"成为可断言事实。
"""

from collections.abc import Mapping
from typing import Final

from xiaowei_agent.contracts import RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True
"""供 tests/security/test_fake_isolation.py 断言；生产模块不得导入本模块。"""

RecordingKey = tuple[str, str | None]


class RecordingNotFoundError(LookupError):
    """调用内容没有对应的回放条目。

    **不回退到某个默认响应**：静默回退会让一条本该被发现的"测试没覆盖这个组合"
    伪装成一次成功取数。
    """


class StarRocksRecordingAdapter:
    """按 ``(operation, database)`` 回放固定审计行。"""

    def __init__(self, recording: Mapping[RecordingKey, AdapterResponse]) -> None:
        if not recording:
            raise ValueError("StarRocksRecordingAdapter requires at least one entry")
        self._recording = dict(recording)
        self.call_count = 0
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, *, context: RequestContext) -> AdapterResponse:
        self.calls.append(call)
        self.call_count += 1
        database = call.typed_args.get("database")
        key: RecordingKey = (call.operation, database if isinstance(database, str) else None)
        try:
            return self._recording[key]
        except KeyError:
            # 不回显 key：operation 与 database 来自调用方。
            raise RecordingNotFoundError("no recording for this call") from None

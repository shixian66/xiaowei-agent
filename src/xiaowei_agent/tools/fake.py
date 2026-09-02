"""fake / recording adapter（DEVELOPMENT_PLAN §7 M2 明文要求置于 tools/）。

**不触达任何被管运维目标**：只回放内存中的固定 payload。``call_count`` 使
"adapter 调用次数为 0"成为可断言事实（ADR-007 D7 承重断言 1 与 4）。
"""

from collections.abc import Sequence
from typing import Final

from xiaowei_agent.contracts import RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True
"""供 tests/security/test_fake_isolation.py 断言；生产模块不得导入本模块。"""


class RecordingToolAdapter:
    def __init__(self, responses: Sequence[AdapterResponse]) -> None:
        if not responses:
            raise ValueError("RecordingToolAdapter requires at least one response")
        self._responses = list(responses)
        self.call_count = 0
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, *, context: RequestContext) -> AdapterResponse:
        self.calls.append(call)
        self.call_count += 1
        index = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[index]

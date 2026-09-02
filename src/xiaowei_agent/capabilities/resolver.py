"""候选生成的唯一契约。实现留到 M3。

``CapabilityResolver`` 是唯一候选生成真源；``route_shadow`` 只能消费同一份
``CandidateSet`` 做 record-only 对比，不得自行 build candidates。
"""

from typing import Protocol

from xiaowei_agent.contracts import (
    CandidateSet,
    CapabilitySnapshot,
    IntentDraft,
    RequestContext,
)


class CapabilityResolver(Protocol):
    def resolve(
        self,
        *,
        draft: IntentDraft,
        context: RequestContext,
        snapshot: CapabilitySnapshot,
    ) -> CandidateSet: ...

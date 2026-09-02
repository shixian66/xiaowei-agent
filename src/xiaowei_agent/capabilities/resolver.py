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


class CapabilityRegistry(Protocol):
    """能力声明与版本索引。

    Registry 只负责给出**某一时刻的快照**；它不是"关键词总表"，也不是执行器。
    Resolver 与分类派生都只看快照，不直接向 Registry 提问——这样同一次请求内
    看到的能力集合是确定的。
    """

    def snapshot(self) -> CapabilitySnapshot: ...


class CapabilityResolver(Protocol):
    def resolve(
        self,
        *,
        draft: IntentDraft,
        context: RequestContext,
        snapshot: CapabilitySnapshot,
    ) -> CandidateSet: ...

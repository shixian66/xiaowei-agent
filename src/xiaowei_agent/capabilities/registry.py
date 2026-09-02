"""静态能力 Registry。

Registry 是**声明与版本索引**，不是关键词总表，也不是执行器。它只给出某一时刻
的 ``CapabilitySnapshot``；Resolver 与分类派生都只看快照，因此同一次请求内看到
的能力集合是确定的。

M3 只注册一个能力。合成写能力 ``test.synthetic.write`` 只存在于测试夹具，
**永远不进入本 registry**（`test_synthetic_write_capability_is_not_registered`
承重）。
"""

from typing import Final

from xiaowei_agent.capabilities.specs import SLOW_QUERY_SPEC
from xiaowei_agent.contracts import CapabilitySnapshot

SNAPSHOT_ID: Final[str] = "snapshot.m3.starrocks.slow_query.v1"
"""快照标识。

写成常量而不是按时刻生成：``snapshot_id`` 会进入 ``CandidateSet`` 与审计，
随调用时刻变化会让"同一输入产生同一计划"在快照这一层就不成立。声明集合变化时
必须显式改这个值并过评审。
"""

_SNAPSHOT: Final[CapabilitySnapshot] = CapabilitySnapshot(
    snapshot_id=SNAPSHOT_ID, specs=(SLOW_QUERY_SPEC,)
)


class StaticCapabilityRegistry:
    """从代码内声明产出快照的 Registry。

    快照是模块级不可变常量，因此 ``snapshot()`` 的返回值在实例之间与调用之间都
    相同——这正是 Resolver 所依赖的确定性。
    """

    def snapshot(self) -> CapabilitySnapshot:
        return _SNAPSHOT

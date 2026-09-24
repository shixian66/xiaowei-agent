"""加载回执与测试结果的**形状**。

放在 ``contracts`` 而不是 ``application``：这两个形状同时被三层用到——application
的状态机拿它判定，persistence 的两个 store 拿它落库和读出，interfaces 的加载器拿它
产出。``persistence`` 只允许依赖 ``contracts``，因此放在别处会立刻违反分层；而让
persistence 自己抄一份行形状，正是这条链路上最容易出现的漂移。
"""

from typing import ClassVar, Literal, TypeAlias

from pydantic import Field

from xiaowei_agent.contracts.base import AwareDatetime, Contract, StrictInt
from xiaowei_agent.contracts.integration_config import ConfigDomain

LoadStatus = Literal["loaded", "invalid"]
TestStatus = Literal["passed", "failed"]

ReceiptKey: TypeAlias = tuple[str, ConfigDomain]
"""加载回执的唯一键 ``(service_name, config_domain)``。

与 ``service_config_state`` 的联合主键逐列相同；W4a 起第二列是配置域而不是 Provider。
"""


class LoadReceipt(Contract):
    """某个进程在某一代配置上的加载结果。

    ``generation`` 恒 ``> 0``：只有真的读到一份可信配置才谈得上"加载过第几代"。
    文件缺失或整体损坏时读不出代次，此时**不产生回执**——缺回执在状态机里本就
    等价于"尚未加载当前 generation"，硬凑一个代次等于伪造证据。
    """

    generation: StrictInt = Field(gt=0)
    status: LoadStatus


class TestResult(Contract):
    """某个测试项在某一代配置上的结果。"""

    # 名字以 Test 开头，pytest 会尝试把它当测试类收集。这一行只是关掉收集。
    __test__: ClassVar[bool] = False

    status: TestStatus
    generation: StrictInt
    tested_at: AwareDatetime | None = None


__all__ = [
    "LoadReceipt",
    "LoadStatus",
    "ReceiptKey",
    "TestResult",
    "TestStatus",
]

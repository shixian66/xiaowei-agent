"""加载回执与页面状态的纯逻辑。

回执的键是 ``(service_name, provider)``——与 ``service_config_state`` 的联合主键、
以及页面状态计算的 ``receipts`` 形状**完全一致**。三处各用一套形状是这条链路上最容易
出现的漂移，因此只在这里定义一次。
"""

from typing import Final, Literal

from pydantic import Field

from xiaowei_agent.contracts import Contract, StrictInt

LoadStatus = Literal["loaded", "invalid"]

SERVICE_WORKER: Final[str] = "worker"
SERVICE_FEISHU_LISTENER: Final[str] = "feishu_listener"
SERVICE_CHANNEL_WORKER: Final[str] = "channel_worker"
SERVICE_WEB: Final[str] = "web"


class LoadReceipt(Contract):
    """某个进程在某一代配置上的加载结果。

    ``generation`` 恒 ``> 0``：只有真的读到一份可信配置才谈得上"加载过第几代"。
    文件缺失或整体损坏时读不出代次，此时**不产生回执**——缺回执在状态机里本就
    等价于"尚未加载当前 generation"，硬凑一个代次等于伪造证据。
    """

    generation: StrictInt = Field(gt=0)
    status: LoadStatus


__all__ = [
    "SERVICE_CHANNEL_WORKER",
    "SERVICE_FEISHU_LISTENER",
    "SERVICE_WEB",
    "SERVICE_WORKER",
    "LoadReceipt",
    "LoadStatus",
]

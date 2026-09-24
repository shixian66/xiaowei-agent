"""加载回执与页面状态的纯逻辑。

回执的键是 ``(service_name, config_domain)``——与 ``service_config_state`` 的联合主键、
以及页面状态计算的 ``receipts`` 形状**完全一致**。三处各用一套形状是这条链路上最容易
出现的漂移，因此只在这里定义一次。
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from xiaowei_agent.contracts import (
    AiConfig,
    ConfigDomain,
    FeishuConfig,
    LoadReceipt,
    ReceiptKey,
    TestResult,
)

UNCONFIGURED_GENERATION: Final[int] = 0
"""文件不存在时的**逻辑**代次。

只活在内存里，不写进文件——每个域文档的 ``generation`` 仍恒 ``> 0``。
干净部署只建三个域目录，此时域文件不存在；第一次保存需要一个起点，否则整条闭环
卡死在第一步。第一次成功保存写 ``0 + 1 = 1``。各域的代次互不相干。
"""

SERVICE_WORKER: Final[str] = "worker"
SERVICE_FEISHU_LISTENER: Final[str] = "feishu_listener"
SERVICE_CHANNEL_WORKER: Final[str] = "channel_worker"
SERVICE_WEB: Final[str] = "web"


class ProviderDisplayState(StrEnum):
    """页面状态闭集；**恰好五个成员**，顺序即判定顺序。

    已接受设计只承诺这五态。``load_status`` 里的 ``invalid`` **不产生第六态**：
    "读了但没读成"本就被"尚未加载当前 generation"涵盖，该列只供页面显示原因提示。
    """

    UNCONFIGURED = "unconfigured"
    PENDING_RESTART = "pending_restart"
    PENDING_TEST = "pending_test"
    AVAILABLE = "available"
    TEST_FAILED = "test_failed"


def current_generation(config: AiConfig | FeishuConfig | None) -> int:
    """把"文件不存在"折成逻辑代次 ``0``。

    只有 ``None`` 走这条路。符号链接、权限、损坏一律在读取层抛错，不会到这里——
    "不存在"是未配置，"存在但坏"是故障，两者不能混。
    """
    return UNCONFIGURED_GENERATION if config is None else config.generation


def _every_required_service_loaded(
    *,
    domain: ConfigDomain,
    generation: int,
    required_service_names: frozenset[str],
    receipts: Mapping[ReceiptKey, LoadReceipt],
) -> bool:
    """``required_service_names`` 中每一个服务都报了当前代次的 ``loaded``。

    缺回执与 ``invalid`` 一视同仁：两者都表示"这个服务还没在跑当前这一代配置"。
    空集为真——没有服务需要它时不得永久停在"待应用"。
    """
    return all(
        (receipt := receipts.get((service_name, domain))) is not None
        and receipt.status == "loaded"
        and receipt.generation == generation
        for service_name in required_service_names
    )


def web_holds_feishu_generation(
    receipts: Mapping[ReceiptKey, LoadReceipt], generation: int
) -> bool:
    """Web 进程的飞书 OAuth adapter 是否正持有 ``generation`` 这一代凭据。

    OAuth 连接测试跨一次浏览器回调，交换 code 用的是 Web **启动期**装配的 adapter，
    而不是回调时的文件。因此开始与回调两端都只认 ``(web, feishu)`` 的 ``loaded`` 回执：
    文件已保存、Web 未重启时，测到的是旧凭据，不能被记到新代次。
    """
    return _every_required_service_loaded(
        domain=ConfigDomain.FEISHU,
        generation=generation,
        required_service_names=frozenset({SERVICE_WEB}),
        receipts=receipts,
    )


def compute_display_state(
    *,
    domain: ConfigDomain,
    enabled: bool,
    required_fields_present: bool,
    current_generation: int,
    required_service_names: frozenset[str],
    receipts: Mapping[ReceiptKey, LoadReceipt],
    test: TestResult | None,
) -> ProviderDisplayState:
    """按设计的五步顺序短路返回，不做任何合并或猜测。

    ``required_service_names`` 由调用方从 ``Settings`` 的装配开关算出：状态机自己
    看不出"本次部署里谁需要这个 Provider"，而没有这个闭集就分不清"某个已启用服务
    缺回执"与"根本没有服务需要它"——后者若被当成前者，页面会永久停在"待应用"。
    """
    if not enabled or not required_fields_present:
        return ProviderDisplayState.UNCONFIGURED
    if not _every_required_service_loaded(
        domain=domain,
        generation=current_generation,
        required_service_names=required_service_names,
        receipts=receipts,
    ):
        return ProviderDisplayState.PENDING_RESTART
    if test is None or test.generation != current_generation:
        return ProviderDisplayState.PENDING_TEST
    if test.status == "passed":
        return ProviderDisplayState.AVAILABLE
    return ProviderDisplayState.TEST_FAILED


__all__ = [
    "SERVICE_CHANNEL_WORKER",
    "SERVICE_FEISHU_LISTENER",
    "SERVICE_WEB",
    "SERVICE_WORKER",
    "UNCONFIGURED_GENERATION",
    "ProviderDisplayState",
    "compute_display_state",
    "current_generation",
    "web_holds_feishu_generation",
]

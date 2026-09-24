"""从本进程挂载的**配置域文件**读出实际要用的 Provider 凭据。

W4a 起每个进程只读自己的域：worker 读 AI 域，listener/channel-worker 读飞书域，Web 只在
启用 OAuth 时读飞书域。W4b 起 worker 另外读 resources 域，但**只**为了签
``(worker, resources)`` 回执：解析完即丢弃，资源参数不进返回值，不交给 Runtime、
Resolver、Registry、ToolGateway 或任何 adapter，也不解析 DNS、不连接任何目标。
进程看不见也不读其他域，更不读旧 ``integrations.json``——运行时
没有双读、回落或自动迁移。

**双层与关系**：`.env` 的装配开关决定这个进程里有没有这条链路，域文件的 ``enabled``
决定运维是否打开它。域文件不能反向启动 Compose 里没装配的进程——否则一份配置文件
就能让一个本不该存在的出站链路活过来。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, TypeVar

from xiaowei_agent.application.integration_state import (
    SERVICE_CHANNEL_WORKER,
    SERVICE_FEISHU_LISTENER,
    SERVICE_WEB,
    SERVICE_WORKER,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AiConfig,
    ConfigDomain,
    FeishuConfig,
    LoadReceipt,
    ReceiptKey,
)
from xiaowei_agent.contracts.resource_config import ResourcesConfig
from xiaowei_agent.interfaces.integration_config_file import (
    DEFAULT_AI_CONFIG_PATH,
    DEFAULT_FEISHU_CONFIG_PATH,
    DEFAULT_RESOURCES_CONFIG_PATH,
    read_ai_config,
    read_feishu_config,
    read_resources_config,
)

_ConfigT = TypeVar("_ConfigT", AiConfig, FeishuConfig, ResourcesConfig)


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    """本进程可用的明文凭据。

    **刻意不用 ``Contract``**：Pydantic 模型默认的 ``repr`` 与 ``model_dump()`` 会带上
    字段值，明文 Key 一旦进异常链、日志或调试输出就泄露。frozen dataclass 加
    ``repr=False`` 让 ``repr()`` 只显示 ``app_id``，两个 secret 不出现；``app_id``
    不是 secret，保留在 ``repr`` 里便于排障。
    """

    gemini_api_key: str | None = field(default=None, repr=False)
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = field(default=None, repr=False)


def required_services(settings: Settings) -> Mapping[ConfigDomain, tuple[str, ...]]:
    """本次部署实际启用、且需要某个域的服务名。

    未启用的服务不写回执——否则页面会永远显示一个没人会去加载的「待应用」。
    resources 域（W4b）恒由 task worker 读取并签回执：worker 没有独立开关，它总在部署里。
    worker 只证明"格式已被读到"，不把资源交给 Runtime、Registry 或任何 adapter。
    """
    ai: list[str] = []
    feishu: list[str] = []
    if settings.gemini_enabled:
        ai.append(SERVICE_WORKER)
    if settings.feishu_listener_enabled:
        feishu.append(SERVICE_FEISHU_LISTENER)
    if settings.channel_worker_enabled:
        feishu.append(SERVICE_CHANNEL_WORKER)
    if settings.feishu_oauth_enabled:
        feishu.append(SERVICE_WEB)
    return {
        ConfigDomain.AI: tuple(ai),
        ConfigDomain.FEISHU: tuple(feishu),
        ConfigDomain.RESOURCES: (SERVICE_WORKER,),
    }


def required_services_for_check(settings: Settings, check_name: str) -> frozenset[str]:
    """某个**测试项**要等哪些服务加载当前 generation。

    两个飞书测试项共用飞书域的加载回执，但等的服务不同——``feishu_oauth`` 只活在 Web
    进程里，listener 有没有重启和它无关。
    """
    services = required_services(settings)
    if check_name == "gemini_connection":
        return frozenset(services[ConfigDomain.AI])
    if check_name == "feishu_credentials":
        return frozenset(services[ConfigDomain.FEISHU])
    if check_name == "feishu_oauth":
        return frozenset({SERVICE_WEB} if settings.feishu_oauth_enabled else ())
    raise ValueError("unknown check name")


def _read_domain(reader: Callable[[str], _ConfigT], path: str) -> _ConfigT | None:
    """文件缺失、断链、权限、非正规文件、schema 非法——全部落到"读不出可信 generation"。"""
    try:
        return reader(path)
    except Exception:
        return None


def load_provider_credentials(
    *,
    settings: Settings,
    service_name: str | None,
    ai_path: str = DEFAULT_AI_CONFIG_PATH,
    feishu_path: str = DEFAULT_FEISHU_CONFIG_PATH,
    resources_path: str = DEFAULT_RESOURCES_CONFIG_PATH,
) -> tuple[ProviderCredentials, Mapping[ReceiptKey, LoadReceipt]]:
    """只读**本服务**消费的域，同时产出本进程的凭据与它自己要写的加载回执。

    ``service_name`` 是调用方的身份声明，**没有默认值**：一条回执的含义是"服务 S 正在
    跑某个域的第 N 代配置"，只有身为 S 的那个进程能作证；传 ``None`` 表示本次装配不为
    任何服务作证，结果是空凭据、空回执，也不读任何文件。

    三种情况都**不抛异常、不阻止进程启动**：

    ========================================  ========  ====================
    情况                                      凭据      回执
    ========================================  ========  ====================
    域文件可读、该 Provider 字段齐备且启用    有值      ``loaded``
    域文件可读、字段缺失或未启用              ``None``  ``invalid``
    **域文件缺失或整体损坏**                  ``None``  **不写回执**
    ========================================  ========  ====================

    第三行是关键：读不出文件就没有可信 generation，而 ``LoadReceipt.generation`` 恒
    ``> 0``，硬凑一个值等于伪造证据。
    """
    if service_name is None:
        return ProviderCredentials(), {}
    required = required_services(settings)
    receipts: dict[ReceiptKey, LoadReceipt] = {}
    gemini_api_key: str | None = None
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None

    if service_name in required[ConfigDomain.AI]:
        ai = _read_domain(read_ai_config, ai_path)
        if ai is not None:
            usable = ai.gemini.enabled and ai.gemini.api_key is not None
            receipts[(service_name, ConfigDomain.AI)] = LoadReceipt(
                generation=ai.generation, status="loaded" if usable else "invalid"
            )
            if usable:
                gemini_api_key = ai.gemini.api_key

    if service_name in required[ConfigDomain.FEISHU]:
        feishu = _read_domain(read_feishu_config, feishu_path)
        if feishu is not None:
            usable = (
                feishu.feishu.enabled
                and feishu.feishu.app_id is not None
                and feishu.feishu.app_secret is not None
            )
            receipts[(service_name, ConfigDomain.FEISHU)] = LoadReceipt(
                generation=feishu.generation, status="loaded" if usable else "invalid"
            )
            if usable:
                feishu_app_id = feishu.feishu.app_id
                feishu_app_secret = feishu.feishu.app_secret

    if service_name in required[ConfigDomain.RESOURCES]:
        # 只取代次：解析成功即 ``loaded``，随即丢弃文档本身。W4b 没有"可用"与否之分，
        # 因为没有任何消费者——回执只说明这一代格式已被 worker 读到。
        resources = _read_domain(read_resources_config, resources_path)
        if resources is not None:
            receipts[(service_name, ConfigDomain.RESOURCES)] = LoadReceipt(
                generation=resources.generation, status="loaded"
            )
        del resources

    return (
        ProviderCredentials(
            gemini_api_key=gemini_api_key,
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
        ),
        receipts,
    )


__all__: Final = [
    "ProviderCredentials",
    "load_provider_credentials",
    "required_services",
    "required_services_for_check",
]

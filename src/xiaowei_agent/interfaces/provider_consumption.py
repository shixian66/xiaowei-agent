"""从 `integrations.json` 读出本进程实际要用的 Provider 凭据。

**双层与关系**：`.env` 的装配开关决定这个进程里有没有这条链路，JSON 的 ``enabled``
决定运维是否打开它。JSON 不能反向启动 Compose 里没装配的进程——否则一份配置文件
就能让一个本不该存在的出站链路活过来。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from xiaowei_agent.application.integration_state import (
    SERVICE_CHANNEL_WORKER,
    SERVICE_FEISHU_LISTENER,
    SERVICE_WEB,
    SERVICE_WORKER,
    LoadReceipt,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import IntegrationConfig, ProviderName
from xiaowei_agent.interfaces.integration_config_file import (
    DEFAULT_INTEGRATION_CONFIG_PATH,
    IntegrationConfigMissingError,
    read_integration_config,
)


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


def read_or_absent(path: str) -> IntegrationConfig | None:
    """只有 ENOENT 才算「尚未配置」；符号链接、权限、损坏一律 fail-closed 往上抛。

    **不要**写成 ``if not os.path.exists(path): return None``：``exists()`` 跟随符号
    链接，指向不存在目标的断链会返回 ``False``，于是它被当成「尚未配置」，直接绕过
    「符号链接一律故障」的设计。判定统一由加载器在 ``os.open`` 的 errno 上做。
    """
    try:
        return read_integration_config(path)
    except IntegrationConfigMissingError:
        return None


def _required_services(settings: Settings) -> Mapping[ProviderName, tuple[str, ...]]:
    """本进程实际启用、且需要某个 Provider 的服务名。

    一个进程可能同时装配了多条链路（例如 Web 既要飞书又要 Gemini），因此是多对多。
    未启用的服务不写回执——否则页面会永远显示一个没人会去加载的「待应用」。
    """
    gemini: list[str] = []
    feishu: list[str] = []
    if settings.gemini_enabled:
        gemini.append(SERVICE_WORKER)
    if settings.feishu_listener_enabled:
        feishu.append(SERVICE_FEISHU_LISTENER)
    if settings.channel_worker_enabled:
        feishu.append(SERVICE_CHANNEL_WORKER)
    if settings.feishu_oauth_enabled:
        feishu.append(SERVICE_WEB)
    return {
        ProviderName.GEMINI: tuple(gemini),
        ProviderName.FEISHU: tuple(feishu),
    }


def _gemini_credential(config: IntegrationConfig) -> str | None:
    if not config.gemini.enabled:
        return None
    return config.gemini.api_key


def _feishu_credential(config: IntegrationConfig) -> tuple[str | None, str | None]:
    if not config.feishu.enabled:
        return None, None
    if config.feishu.app_id is None or config.feishu.app_secret is None:
        return None, None
    return config.feishu.app_id, config.feishu.app_secret


def load_provider_credentials(
    *, settings: Settings, path: str = DEFAULT_INTEGRATION_CONFIG_PATH
) -> tuple[ProviderCredentials, Mapping[tuple[str, str], LoadReceipt]]:
    """读一次文件，同时产出本进程的凭据与它要写的加载回执。

    三种情况都**不抛异常、不阻止进程启动**：

    ========================================  ======  ====================
    情况                                      凭据    回执
    ========================================  ======  ====================
    文件可读、该 Provider 字段齐备            有值    ``loaded``
    文件可读、该 Provider 字段缺失或非法      ``None``  ``invalid``
    **文件缺失或整体损坏**                    ``None``  **不写回执**
    ========================================  ======  ====================

    第三行是关键：读不出文件就没有可信 generation，而 ``LoadReceipt.generation``
    恒 ``> 0``，硬凑一个值等于伪造证据。缺回执在状态机里本就等价于「尚未加载当前
    generation」，页面仍会正确显示「待应用」。
    """
    required = _required_services(settings)
    config: IntegrationConfig | None = None
    try:
        config = read_or_absent(path)
    except Exception:
        # 断链、权限、非正规文件、schema 非法——全部落到"读不出可信 generation"。
        config = None
    if config is None:
        return ProviderCredentials(), {}

    gemini_key = _gemini_credential(config)
    feishu_app_id, feishu_app_secret = _feishu_credential(config)
    available: dict[ProviderName, bool] = {
        ProviderName.GEMINI: gemini_key is not None,
        ProviderName.FEISHU: feishu_app_secret is not None,
    }
    receipts: dict[tuple[str, str], LoadReceipt] = {
        (service_name, provider.value): LoadReceipt(
            generation=config.generation,
            status="loaded" if available[provider] else "invalid",
        )
        for provider, service_names in required.items()
        for service_name in service_names
    }
    return (
        ProviderCredentials(
            gemini_api_key=gemini_key if required[ProviderName.GEMINI] else None,
            feishu_app_id=feishu_app_id if required[ProviderName.FEISHU] else None,
            feishu_app_secret=(
                feishu_app_secret if required[ProviderName.FEISHU] else None
            ),
        ),
        receipts,
    )


__all__: Final = [
    "ProviderCredentials",
    "load_provider_credentials",
    "read_or_absent",
]

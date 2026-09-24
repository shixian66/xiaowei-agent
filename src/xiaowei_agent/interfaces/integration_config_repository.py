"""application ``IntegrationConfigRepository`` port 的文件 adapter。

与 :mod:`xiaowei_agent.interfaces.integration_config_file` 分开放，是为了让依赖方向只
出现在**写配置的那一个进程**里：consumer（worker、飞书 listener、channel worker）只经
``integration_config_file`` 读自己的域，从不 import application 的配置写服务；只有
web-app 的 composition root 装配本模块。``_conformance.py`` 静态锚定它与 port 的
结构兼容，application 绝不反向 import 这里。
"""

from xiaowei_agent.application.integration_config_service import (
    IntegrationConfigUnreadableError,
    IntegrationConfigWriteError,
)
from xiaowei_agent.contracts import AiConfig, FeishuConfig
from xiaowei_agent.interfaces.integration_config_file import (
    DEFAULT_AI_CONFIG_PATH,
    DEFAULT_FEISHU_CONFIG_PATH,
    IntegrationConfigError,
    IntegrationConfigMissingError,
    read_ai_config,
    read_feishu_config,
    write_ai_config,
    write_feishu_config,
)


class FileIntegrationConfigRepository:
    """application ``IntegrationConfigRepository`` 的文件 adapter。

    只把本模块的窄入口翻译成 application 的闭集错误：不存在是 ``None``，存在但坏是
    :class:`IntegrationConfigUnreadableError`，写失败是 :class:`IntegrationConfigWriteError`。
    路径在构造时由 composition root 固定，方法不接收路径、schema 或 domain。
    """

    def __init__(
        self,
        *,
        ai_path: str = DEFAULT_AI_CONFIG_PATH,
        feishu_path: str = DEFAULT_FEISHU_CONFIG_PATH,
    ) -> None:
        self._ai_path = ai_path
        self._feishu_path = feishu_path

    def read_ai(self) -> AiConfig | None:
        try:
            return read_ai_config(self._ai_path)
        except IntegrationConfigMissingError:
            return None
        except IntegrationConfigError:
            raise IntegrationConfigUnreadableError from None

    def write_ai(self, *, config: AiConfig) -> None:
        try:
            write_ai_config(self._ai_path, config)
        except IntegrationConfigError:
            raise IntegrationConfigWriteError from None

    def read_feishu(self) -> FeishuConfig | None:
        try:
            return read_feishu_config(self._feishu_path)
        except IntegrationConfigMissingError:
            return None
        except IntegrationConfigError:
            raise IntegrationConfigUnreadableError from None

    def write_feishu(self, *, config: FeishuConfig) -> None:
        try:
            write_feishu_config(self._feishu_path, config)
        except IntegrationConfigError:
            raise IntegrationConfigWriteError from None


__all__ = ["FileIntegrationConfigRepository"]

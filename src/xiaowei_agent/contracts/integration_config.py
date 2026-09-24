"""三域配置文件的严格契约：Provider 明文凭据的唯一真源。

W4a 起 AI 与飞书各自一份固定文件（``ai/config.json`` / ``feishu/config.json``），
各自一个 ``generation``；resources 域只预留名字与挂载，W4b 才有文档契约。
旧的组合文档 :class:`IntegrationConfig` **只是一次性迁移的输入契约**：运行时 consumer
与 Web 路由都不得再引用它（``tests/contract/test_w4_config_domain_contracts.py``）。

Secret 字段必须**同时**写 ``exclude=True`` 与 ``repr=False``，取值只能过
:meth:`GeminiIntegration.secret_value` / :meth:`FeishuIntegration.secret_value`。

**两个标志各挡一条通道，缺一不可**：``exclude=True`` 只作用于 ``model_dump()``
与 ``model_dump_json()``；``repr()`` 和 ``str()`` 走的是另一条路径，不受它影响。
只写 ``exclude=True`` 的对象一旦进异常链、调试日志或测试失败输出，完整 Key 会被
原样打印出来。这条义务挂在类型上而不是字段名上——凡是标注为 ``Secret*`` 的字段
都必须两个标志齐全，由 ``tests/security/test_secret_field_exposure.py`` 机械核对。
"""

import unicodedata
from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, Field

from xiaowei_agent.contracts.base import Contract, StrictInt, StrictStr

_MAX_SECRET_LENGTH: Final[int] = 4096


def _secret_ref(value: str) -> str:
    if not value or len(value) > _MAX_SECRET_LENGTH:
        raise ValueError("must be a bounded non-empty secret")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("must be a bounded non-empty secret")
    return value


SecretRef = Annotated[StrictStr, AfterValidator(_secret_ref)]


class GeminiIntegration(Contract):
    """Gemini 的启用开关与 API Key。"""

    enabled: bool = False
    api_key: SecretRef | None = Field(default=None, exclude=True, repr=False)

    def secret_value(self) -> str:
        """显式读取 API Key；未配置时抛 ``ValueError``。"""
        if self.api_key is None:
            raise ValueError("gemini api key is not configured")
        return self.api_key


class FeishuIntegration(Contract):
    """飞书的启用开关、App ID 与 App Secret。"""

    enabled: bool = False
    app_id: StrictStr | None = None
    app_secret: SecretRef | None = Field(default=None, exclude=True, repr=False)

    def secret_value(self) -> str:
        """显式读取 App Secret；未配置时抛 ``ValueError``。"""
        if self.app_secret is None:
            raise ValueError("feishu app secret is not configured")
        return self.app_secret


class ConfigDomain(StrEnum):
    """配置域闭集：一个宿主目录、一份固定文件、一个独立 ``generation``。

    加载回执的键、``service_config_state.config_domain`` 的 CHECK 与 Web 投影都用它，
    不再借 :class:`~xiaowei_agent.contracts.enums.ProviderName` 冒充配置域——后者只服务
    现有 Gemini/飞书探针分类。``RESOURCES`` 在 W4a 只占名字与挂载，没有文档契约。
    """

    AI = "ai"
    FEISHU = "feishu"
    RESOURCES = "resources"


class AiConfig(Contract):
    """AI 域的完整文档。``generation`` 恒 ``> 0``，每次成功保存自增。"""

    generation: StrictInt = Field(gt=0)
    gemini: GeminiIntegration


class FeishuConfig(Contract):
    """飞书域的完整文档；与 AI 域的代次互不相干。"""

    generation: StrictInt = Field(gt=0)
    feishu: FeishuIntegration


class IntegrationConfig(Contract):
    """旧 ``integrations.json`` 的组合文档，**只作一次性迁移的输入**。

    运行时不得再读它、写它或据它判断状态；迁移器把它映射成同一代次的
    :class:`AiConfig` 与 :class:`FeishuConfig` 后即删除旧文件。
    """

    generation: StrictInt = Field(gt=0)
    gemini: GeminiIntegration
    feishu: FeishuIntegration


__all__ = [
    "AiConfig",
    "ConfigDomain",
    "FeishuConfig",
    "FeishuIntegration",
    "GeminiIntegration",
    "IntegrationConfig",
    "SecretRef",
]

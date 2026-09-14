"""`integrations.json` 的严格契约：Provider 明文凭据的唯一真源。

Secret 字段用 ``Field(exclude=True)`` 从 ``model_dump()`` 里摘掉，取值只能过
:meth:`GeminiIntegration.secret_value` / :meth:`FeishuIntegration.secret_value`。
默认序列化会进日志、错误响应与调试输出，把 secret 留在默认路径上等于把它交给
所有这些通道；显式访问器让每一次读取都在源码里看得见。
"""

import unicodedata
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
    api_key: SecretRef | None = Field(default=None, exclude=True)

    def secret_value(self) -> str:
        """显式读取 API Key；未配置时抛 ``ValueError``。"""
        if self.api_key is None:
            raise ValueError("gemini api key is not configured")
        return self.api_key


class FeishuIntegration(Contract):
    """飞书的启用开关、App ID 与 App Secret。"""

    enabled: bool = False
    app_id: StrictStr | None = None
    app_secret: SecretRef | None = Field(default=None, exclude=True)

    def secret_value(self) -> str:
        """显式读取 App Secret；未配置时抛 ``ValueError``。"""
        if self.app_secret is None:
            raise ValueError("feishu app secret is not configured")
        return self.app_secret


class IntegrationConfig(Contract):
    """一份完整配置。``generation`` 恒 ``> 0``，每次成功保存自增。"""

    generation: StrictInt = Field(gt=0)
    gemini: GeminiIntegration
    feishu: FeishuIntegration


__all__ = [
    "FeishuIntegration",
    "GeminiIntegration",
    "IntegrationConfig",
    "SecretRef",
]

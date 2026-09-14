"""`integrations.json` 的严格契约：Provider 明文凭据的唯一真源。

Secret 字段必须**同时**写 ``exclude=True`` 与 ``repr=False``，取值只能过
:meth:`GeminiIntegration.secret_value` / :meth:`FeishuIntegration.secret_value`。

**两个标志各挡一条通道，缺一不可**：``exclude=True`` 只作用于 ``model_dump()``
与 ``model_dump_json()``；``repr()`` 和 ``str()`` 走的是另一条路径，不受它影响。
只写 ``exclude=True`` 的对象一旦进异常链、调试日志或测试失败输出，完整 Key 会被
原样打印出来。这条义务挂在类型上而不是字段名上——凡是标注为 ``Secret*`` 的字段
都必须两个标志齐全，由 ``tests/security/test_secret_field_exposure.py`` 机械核对。
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

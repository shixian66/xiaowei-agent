"""外部文本的统一包装。

``trust`` 是 ``Literal[TrustLevel.UNTRUSTED]``，因此"把外部文本标为可信"在契约层
不可表达。``digest`` 在校验阶段重算并比对，伪造摘要即校验失败。
"""

import hashlib
from typing import Literal, Self

from pydantic import TypeAdapter, model_validator

from xiaowei_agent.contracts.base import AwareDatetime, Contract, FreeText, Sha256Hex
from xiaowei_agent.contracts.enums import ExternalSource, TrustLevel

_TEXT = TypeAdapter(FreeText)


def content_digest(content: str) -> str:
    """外部文本的 SHA-256 十六进制摘要。

    **先校验再派生**：直接 ``content.encode()`` 会让 ``bytes`` 之类的入参抛
    ``AttributeError`` ——那是解释器级的意外错误，不是契约的 fail-closed 拒绝，
    调用方无法与"真的算不出摘要"区分。Gateway 的异常处理器自身也调用本函数，
    在那里冒出 ``AttributeError`` 会直接逃出已加固的 except 分支。
    """
    return hashlib.sha256(_TEXT.validate_python(content, strict=True).encode("utf-8")).hexdigest()


class ExternalContent(Contract):
    source: ExternalSource
    trust: Literal[TrustLevel.UNTRUSTED] = TrustLevel.UNTRUSTED
    content: FreeText
    digest: Sha256Hex
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def _digest_must_match(self) -> Self:
        if self.digest != content_digest(self.content):
            raise ValueError("digest does not match content")
        return self

    @classmethod
    def capture(
        cls, *, source: ExternalSource, content: str, captured_at: AwareDatetime
    ) -> Self:
        """唯一推荐构造入口：自动计算摘要，调用方无从伪造。"""
        return cls(
            source=source,
            content=content,
            digest=content_digest(content),
            captured_at=captured_at,
        )

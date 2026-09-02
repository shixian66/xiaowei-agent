"""外部文本的统一包装。

``trust`` 是 ``Literal[TrustLevel.UNTRUSTED]``，因此"把外部文本标为可信"在契约层
不可表达。``digest`` 在校验阶段重算并比对，伪造摘要即校验失败。
"""

import datetime as _dt
import hashlib
from typing import Literal, Self

from pydantic import model_validator

from xiaowei_agent.contracts.base import Contract
from xiaowei_agent.contracts.enums import ExternalSource, TrustLevel


def content_digest(content: str) -> str:
    """外部文本的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ExternalContent(Contract):
    source: ExternalSource
    trust: Literal[TrustLevel.UNTRUSTED] = TrustLevel.UNTRUSTED
    content: str
    digest: str
    captured_at: _dt.datetime

    @model_validator(mode="after")
    def _digest_must_match(self) -> Self:
        if self.digest != content_digest(self.content):
            raise ValueError("digest does not match content")
        return self

    @classmethod
    def capture(
        cls, *, source: ExternalSource, content: str, captured_at: _dt.datetime
    ) -> Self:
        """唯一推荐构造入口：自动计算摘要，调用方无从伪造。"""
        return cls(
            source=source,
            content=content,
            digest=content_digest(content),
            captured_at=captured_at,
        )

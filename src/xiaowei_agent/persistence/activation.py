"""身份激活申请的窄持久化边界与摘要规则。"""

import hashlib
from typing import Final, Literal, Protocol

from xiaowei_agent.contracts.activation import (
    ActivationLookup,
    ActivationRequest,
    CreateActivationCommand,
)

ACTIVATION_TTL_SECONDS: Final[int] = 24 * 60 * 60
MAX_PENDING_ACTIVATIONS: Final[int] = 1024

_SUBJECT_DIGEST_DOMAIN: Final[str] = "xiaowei.activation.subject.v1"
_EVENT_DIGEST_DOMAIN: Final[str] = "xiaowei.activation.event.v1"
_CHAT_DIGEST_DOMAIN: Final[str] = "xiaowei.activation.chat.v1"


class ActivationError(RuntimeError):
    """激活事实存储的闭集错误基类。"""


class ActivationCapacityError(ActivationError):
    """有效待办达到固定上限。"""

    def __init__(self) -> None:
        super().__init__("activation capacity is unavailable")


def activation_subject_digest(command: CreateActivationCommand) -> str:
    """申请主体的独立摘要域；不得复用身份绑定表的摘要。"""
    material = (
        f"{_SUBJECT_DIGEST_DOMAIN}\x1f{command.provider.value}\x1f"
        f"{command.tenant_id}\x1f{command.environment_id}\x1f{command.subject_ref}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def activation_source_ref_digest(
    *, kind: Literal["event", "chat"], reference: str
) -> str:
    """把事件与会话引用放进不同摘要域，避免等值引用可关联。"""
    domain = _EVENT_DIGEST_DOMAIN if kind == "event" else _CHAT_DIGEST_DOMAIN
    return hashlib.sha256(f"{domain}\x1f{reference}".encode()).hexdigest()


class ActivationStore(Protocol):
    """只创建/复用/收割待办；没有批准、拒绝或通用更新入口。"""

    async def create_or_reuse(
        self, *, command: CreateActivationCommand
    ) -> ActivationRequest:
        """收割过期项、执行容量门，然后复用或创建一个待办。"""

    async def load(self, *, query: ActivationLookup) -> ActivationRequest | None:
        """按 request id 与显式作用域读取；作用域不匹配按不存在处理。"""


__all__ = [
    "ACTIVATION_TTL_SECONDS",
    "MAX_PENDING_ACTIVATIONS",
    "ActivationCapacityError",
    "ActivationError",
    "ActivationStore",
    "activation_source_ref_digest",
    "activation_subject_digest",
]

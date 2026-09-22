"""群激活状态的单次、幂等、无恢复投递协调。"""

import logging
from typing import Final

from xiaowei_agent.application.channel_projection import (
    ChannelMessageError,
    ChannelMessagePort,
)
from xiaowei_agent.contracts import content_digest
from xiaowei_agent.rendering.feishu import render_activation_card

_LOGGER = logging.getLogger(__name__)
_IDEMPOTENCY_DOMAIN: Final[str] = "xiaowei.activation.notification.v1"


def activation_notification_idempotency_ref(*, event_id: str) -> str:
    """仅从已校验事件 ID 派生稳定、域隔离的重投引用。"""
    return content_digest(f"{_IDEMPOTENCY_DOMAIN}\x1f{event_id}")


class ActivationNotificationService:
    """只发送一次通用卡片；没有 sleep、重试或投递状态。"""

    def __init__(self, *, messages: ChannelMessagePort) -> None:
        self._messages = messages

    async def notify(
        self,
        *,
        conversation_ref: str,
        event_id: str,
        submitted: bool,
    ) -> bool:
        """成功返回真；端口闭集失败只记无敏感值诊断并返回假。"""
        try:
            await self._messages.send_to_chat(
                conversation_ref=conversation_ref,
                card=render_activation_card(submitted=submitted),
                idempotency_ref=activation_notification_idempotency_ref(
                    event_id=event_id
                ),
            )
        except ChannelMessageError:
            try:
                _LOGGER.warning(
                    "activation notification delivery failed",
                    extra={"failure_kind": "channel_message_error"},
                )
            except Exception:
                return False
            return False
        return True


__all__ = [
    "ActivationNotificationService",
    "activation_notification_idempotency_ref",
]

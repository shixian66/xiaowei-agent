"""像 SQL 的飞书消息的单次、幂等、无恢复固定提示（设计 §5.1）。

这类消息在写任何事实前就被拒绝，没有任务可投影，所以由这里直接回一张固定卡片；卡片不回显原文。
"""

import logging
from typing import Final

from xiaowei_agent.application.channel_projection import (
    ChannelMessageError,
    ChannelMessagePort,
)
from xiaowei_agent.contracts import content_digest
from xiaowei_agent.rendering.feishu import render_sql_like_notice_card
from xiaowei_agent.rendering.generic import SQL_LIKE_TEXT_REJECTED

_LOGGER = logging.getLogger(__name__)
_IDEMPOTENCY_DOMAIN: Final[str] = "xiaowei.sql_like.notice.v1"


def sql_like_notice_idempotency_ref(*, event_id: str) -> str:
    """仅从已校验事件 ID 派生稳定、域隔离的重投引用。"""
    return content_digest(f"{_IDEMPOTENCY_DOMAIN}\x1f{event_id}")


class SqlLikeNoticeService:
    """只发送一次固定提示卡片；没有 sleep、重试或投递状态。"""

    def __init__(self, *, messages: ChannelMessagePort) -> None:
        self._messages = messages

    async def notify(self, *, conversation_ref: str, event_id: str) -> bool:
        """成功返回真；端口闭集失败只记无敏感值诊断并返回假。"""
        try:
            await self._messages.send_to_chat(
                conversation_ref=conversation_ref,
                card=render_sql_like_notice_card(body=SQL_LIKE_TEXT_REJECTED),
                idempotency_ref=sql_like_notice_idempotency_ref(event_id=event_id),
            )
        except ChannelMessageError:
            try:
                _LOGGER.warning(
                    "sql-like notice delivery failed",
                    extra={"failure_kind": "channel_message_error"},
                )
            except Exception:
                return False
            return False
        return True


__all__ = ["SqlLikeNoticeService", "sql_like_notice_idempotency_ref"]

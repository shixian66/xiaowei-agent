"""群激活通知只尝试一次，并用事件派生稳定幂等引用。"""

import logging

import pytest

from xiaowei_agent.application.activation_notification import (
    ActivationNotificationService,
)
from xiaowei_agent.application.channel_projection import ChannelMessageError
from xiaowei_agent.contracts import ProjectionErrorCode
from xiaowei_agent.rendering.feishu import RenderedFeishuCard


class _Messages:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, RenderedFeishuCard, str]] = []

    async def send_to_chat(
        self,
        *,
        conversation_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        self.calls.append((conversation_ref, card, idempotency_ref))
        if self.error is not None:
            raise self.error
        return "message-ref"


@pytest.mark.asyncio
async def test_same_event_uses_the_same_single_delivery_reference() -> None:
    messages = _Messages()
    service = ActivationNotificationService(messages=messages)

    assert await service.notify(
        conversation_ref="chat-1", event_id="event-1", submitted=True
    )
    assert await service.notify(
        conversation_ref="chat-1", event_id="event-1", submitted=True
    )

    assert len(messages.calls) == 2
    assert messages.calls[0][2] == messages.calls[1][2]
    assert messages.calls[0][2] != "event-1"


@pytest.mark.asyncio
async def test_replayed_event_keeps_one_reference_when_capacity_state_changes() -> None:
    messages = _Messages()
    service = ActivationNotificationService(messages=messages)

    await service.notify(
        conversation_ref="chat-1", event_id="event-1", submitted=False
    )
    await service.notify(
        conversation_ref="chat-1", event_id="event-1", submitted=True
    )

    assert messages.calls[0][2] == messages.calls[1][2]


@pytest.mark.asyncio
async def test_provider_failure_is_logged_without_sensitive_values_and_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _Messages(
        ChannelMessageError(error_code=ProjectionErrorCode.PROVIDER_UNAVAILABLE)
    )
    service = ActivationNotificationService(messages=messages)

    with caplog.at_level(logging.WARNING):
        delivered = await service.notify(
            conversation_ref="private-chat-reference",
            event_id="private-event-reference",
            submitted=True,
        )

    assert delivered is False
    assert len(messages.calls) == 1
    combined = " ".join(record.getMessage() for record in caplog.records)
    assert "private-chat-reference" not in combined
    assert "private-event-reference" not in combined
    assert "provider_unavailable" not in combined


@pytest.mark.asyncio
async def test_programming_errors_are_not_disguised_as_delivery_failures() -> None:
    messages = _Messages(TypeError("broken adapter"))
    service = ActivationNotificationService(messages=messages)

    with pytest.raises(TypeError, match="broken adapter"):
        await service.notify(
            conversation_ref="chat-1", event_id="event-1", submitted=False
        )

    assert len(messages.calls) == 1

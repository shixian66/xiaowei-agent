"""飞书消息入口只做协议、身份上下文与统一提交服务委托。"""

import asyncio
import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import cast

import pytest
from tests.fakes.feishu import RecordingFeishuInboundTransport

from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
    SubmittedTask,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    IdentitySource,
)
from xiaowei_agent.interfaces import feishu_listener as feishu_listener_module
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.feishu_listener import FeishuListener, main, serve_listener
from xiaowei_agent.interfaces.feishu_sdk import FeishuMention, FeishuMessageEvent
from xiaowei_agent.persistence.channel import (
    ChannelBindingConflictError,
    ProjectionSubscriptionConflictError,
)
from xiaowei_agent.persistence.store import IdempotencyConflictError

_NOW = dt.datetime(2026, 9, 8, 9, tzinfo=dt.UTC)


class _RecordingSubmissionService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.commands: list[ChannelSubmitCommand] = []
        self.failure = failure

    async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        if ChannelPermission.SUBMIT_READONLY_TASK not in command.principal.permissions:
            raise ChannelSubmissionForbiddenError
        return cast(SubmittedTask, object())


def _principal(
    permissions: frozenset[ChannelPermission] | None = None,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="user-open-id",
        permissions=permissions
        if permissions is not None
        else frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    )


def _event(**updates: object) -> FeishuMessageEvent:
    values: dict[str, object] = {
        "schema": "2.0",
        "event_id": "event-1",
        "event_type": "im.message.receive_v1",
        "app_id": "cli_test_app",
        "tenant_key": "tenant-test",
        "sender_type": "user",
        "sender_subject_ref": "user-open-id",
        "message_id": "message-1",
        "chat_id": "chat-1",
        "chat_type": "p2p",
        "message_type": "text",
        "text": "inspect slow queries",
        "mentions": (),
    }
    return FeishuMessageEvent(**(values | updates))


def _listener(
    service: _RecordingSubmissionService,
    *,
    principal: AuthenticatedPrincipal | None = None,
) -> FeishuListener:
    identity = _principal() if principal is None else principal
    return FeishuListener(
        app_id="cli_test_app",
        tenant_key="tenant-test",
        bot_open_id="bot-open-id",
        tenant_id="dev-local",
        environment_id="dev",
        identity_directory=StaticFeishuIdentityDirectory(
            principals={identity.subject_ref: identity}
        ),
        submission_service=cast(ChannelSubmissionService, service),
        policy_revision="policy-1",
        clock=lambda: _NOW,
        trace_id_factory=lambda: "1" * 32,
    )


async def test_private_text_uses_only_server_owned_identity_and_context() -> None:
    service = _RecordingSubmissionService()

    assert await _listener(service).handle_event(event=_event()) is True

    assert len(service.commands) == 1
    command = service.commands[0]
    assert command.principal.actor == "alice"
    assert command.principal.tenant_id == "dev-local"
    assert command.principal.environment_id == "dev"
    assert command.channel is ChannelKind.FEISHU_PRIVATE
    assert command.client_submission_ref == "event-1"
    assert command.request_id == "feishu:event-1"
    assert command.trace_id == "1" * 32
    assert command.policy_revision == "policy-1"
    assert command.submitted_at == _NOW
    assert command.conversation_ref is None


async def test_group_text_requires_exact_bot_mention_and_removes_only_its_key() -> None:
    service = _RecordingSubmissionService()
    listener = _listener(service)
    mention = FeishuMention(key="@_user_1", subject_ref="bot-open-id")

    assert (
        await listener.handle_event(
            event=_event(
                chat_type="group",
                text="@_user_1 inspect @someone",
                mentions=(mention,),
            )
        )
        is True
    )

    command = service.commands[0]
    assert command.channel is ChannelKind.FEISHU_GROUP
    assert command.conversation_ref == "chat-1"
    assert command.text == " inspect @someone"


async def test_group_removes_only_the_metadata_bound_mention_occurrence() -> None:
    service = _RecordingSubmissionService()
    listener = _listener(service)
    mention = FeishuMention(key="@_user_1", subject_ref="bot-open-id")

    assert (
        await listener.handle_event(
            event=_event(
                chat_type="group",
                text="@_user_1 explain the literal @_user_1 marker",
                mentions=(mention,),
            )
        )
        is True
    )

    assert service.commands[0].text == " explain the literal @_user_1 marker"


async def test_duplicate_mention_metadata_cannot_remove_a_user_literal() -> None:
    service = _RecordingSubmissionService()
    mention = FeishuMention(key="@_user_1", subject_ref="bot-open-id")

    assert (
        await _listener(service).handle_event(
            event=_event(
                chat_type="group",
                text="@_user_1 explain @_user_1",
                mentions=(mention, mention),
            )
        )
        is True
    )

    assert service.commands[0].text == " explain @_user_1"


async def test_group_without_bot_mention_or_only_mention_creates_no_task() -> None:
    service = _RecordingSubmissionService()
    listener = _listener(service)

    assert await listener.handle_event(event=_event(chat_type="group")) is False
    assert (
        await listener.handle_event(
            event=_event(
                chat_type="group",
                text="@_user_1   ",
                mentions=(FeishuMention(key="@_user_1", subject_ref="bot-open-id"),),
            )
        )
        is False
    )
    assert service.commands == []


async def test_group_rejects_bot_mention_metadata_missing_from_text() -> None:
    service = _RecordingSubmissionService()

    assert (
        await _listener(service).handle_event(
            event=_event(
                chat_type="group",
                text="inspect slow queries",
                mentions=(
                    FeishuMention(key="@_user_1", subject_ref="bot-open-id"),
                ),
            )
        )
        is False
    )
    assert service.commands == []


@pytest.mark.parametrize(
    "updates",
    [
        {"app_id": "another-app"},
        {"tenant_key": "another-tenant"},
        {"sender_type": "bot"},
        {"chat_type": "thread"},
        {"message_type": "image", "text": None},
    ],
)
async def test_unrecognized_or_cross_scope_events_are_acknowledged_without_a_task(
    updates: dict[str, object],
) -> None:
    service = _RecordingSubmissionService()

    assert await _listener(service).handle_event(event=_event(**updates)) is False
    assert service.commands == []


async def test_unknown_identity_and_missing_submit_permission_fail_closed() -> None:
    unknown_service = _RecordingSubmissionService()
    no_submit_service = _RecordingSubmissionService()
    no_submit = _principal(
        permissions=frozenset({ChannelPermission.VIEW_SAFE_TASK})
    )

    assert (
        await _listener(unknown_service).handle_event(
            event=_event(sender_subject_ref="unknown-open-id")
        )
        is False
    )
    assert await _listener(no_submit_service, principal=no_submit).handle_event(
        event=_event()
    ) is False
    assert unknown_service.commands == []
    assert len(no_submit_service.commands) == 1


async def test_event_replay_keeps_the_same_service_idempotency_input() -> None:
    service = _RecordingSubmissionService()
    listener = _listener(service)

    assert await listener.handle_event(event=_event(message_id="message-a")) is True
    assert await listener.handle_event(event=_event(message_id="message-b")) is True

    assert [item.client_submission_ref for item in service.commands] == [
        "event-1",
        "event-1",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        IdempotencyConflictError(),
        ChannelBindingConflictError(),
        ProjectionSubscriptionConflictError(),
    ],
)
async def test_permanent_submission_conflicts_are_acknowledged_without_success(
    failure: Exception,
) -> None:
    service = _RecordingSubmissionService(failure=failure)

    assert await _listener(service).handle_event(event=_event()) is False
    assert len(service.commands) == 1


def test_local_event_contract_rejects_oversized_text_and_unknown_fields() -> None:
    with pytest.raises(ValueError):
        _event(text="x" * 8193)
    with pytest.raises(ValueError):
        FeishuMessageEvent(
            **(_event().model_dump(by_alias=True) | {"actor": "root"})
        )


async def test_process_bridge_waits_for_persistence_before_callback_returns() -> None:
    service = _RecordingSubmissionService()
    transport = RecordingFeishuInboundTransport(events=(_event(),))
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    class Stack:
        listener = _listener(service)
        aclose = staticmethod(close)

        def __init__(self) -> None:
            self.transport = transport

    assert await serve_listener(stack=Stack()) == 0
    assert len(service.commands) == 1
    assert transport.callbacks_completed == 1
    assert closed is True


async def test_process_bridge_does_not_ack_persistence_uncertainty() -> None:
    service = _RecordingSubmissionService(failure=RuntimeError("storage unavailable"))
    transport = RecordingFeishuInboundTransport(events=(_event(),))
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    class Stack:
        listener = _listener(service)
        aclose = staticmethod(close)

        def __init__(self) -> None:
            self.transport = transport

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await serve_listener(stack=Stack())

    assert transport.callbacks_completed == 0
    assert closed is True


async def test_process_bridge_cancels_timed_out_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    class HangingSubmissionService:
        async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask:
            del command
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    transport = RecordingFeishuInboundTransport(events=(_event(),))
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    class Stack:
        listener = _listener(
            cast(_RecordingSubmissionService, HangingSubmissionService())
        )
        aclose = staticmethod(close)

        def __init__(self) -> None:
            self.transport = transport

    monkeypatch.setattr(feishu_listener_module, "_CALLBACK_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError):
        await serve_listener(stack=Stack())

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert transport.callbacks_completed == 0
    assert closed is True


async def test_process_bridge_allows_only_one_inflight_callback() -> None:
    class ConcurrentSubmissionService(_RecordingSubmissionService):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum_active = 0

        async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask:
            self.commands.append(command)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0.05)
            finally:
                self.active -= 1
            return cast(SubmittedTask, object())

    class ConcurrentTransport:
        def __init__(self) -> None:
            self.callbacks_completed = 0

        def run_forever(
            self, *, on_event: Callable[[FeishuMessageEvent], None]
        ) -> None:
            barrier = threading.Barrier(3)
            errors: list[Exception] = []
            result_lock = threading.Lock()

            def deliver(event: FeishuMessageEvent) -> None:
                barrier.wait()
                try:
                    on_event(event)
                except Exception as error:
                    with result_lock:
                        errors.append(error)
                else:
                    with result_lock:
                        self.callbacks_completed += 1

            threads = tuple(
                threading.Thread(target=deliver, args=(_event(event_id=f"event-{index}"),))
                for index in range(2)
            )
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            if errors:
                raise errors[0]

    service = ConcurrentSubmissionService()
    transport = ConcurrentTransport()

    async def close() -> None:
        return None

    class Stack:
        listener = _listener(service)
        aclose = staticmethod(close)

        def __init__(self) -> None:
            self.transport = transport

    assert await serve_listener(stack=Stack()) == 0
    assert transport.callbacks_completed == 2
    assert service.maximum_active == 1


def test_process_main_is_default_closed_before_starting_async_runtime(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        feishu_listener_module,
        "load_settings",
        lambda: Settings(environment_id="dev"),
    )

    def fail_run(_: object) -> int:
        raise AssertionError("disabled listener must not enter asyncio runtime")

    monkeypatch.setattr(feishu_listener_module.asyncio, "run", fail_run)

    with caplog.at_level(logging.ERROR):
        assert main() == 2

    assert caplog.records == []

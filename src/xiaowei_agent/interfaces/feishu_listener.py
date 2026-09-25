"""默认关闭的飞书长连接消息入口。"""

import asyncio
import datetime as dt
import logging
import sys
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from enum import StrEnum
from threading import Lock
from typing import Final, Protocol

from xiaowei_agent.application.activation_notification import (
    ActivationNotificationService,
)
from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
)
from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.contracts import ChannelKind
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityDirectory,
    FeishuIdentityNotFoundError,
    FeishuIdentityUnavailableError,
)
from xiaowei_agent.interfaces.feishu_sdk import (
    FeishuInboundTransport,
    FeishuMessageEvent,
)
from xiaowei_agent.log import configure_logging
from xiaowei_agent.persistence.activation import ActivationCapacityError
from xiaowei_agent.persistence.channel import (
    ChannelBindingConflictError,
    ProjectionSubscriptionConflictError,
)
from xiaowei_agent.persistence.store import IdempotencyConflictError
from xiaowei_agent.trace import bind_trace_id, new_trace_id

_LOGGER = logging.getLogger(__name__)
_EVENT_TYPE: Final[str] = "im.message.receive_v1"
_CALLBACK_TIMEOUT_SECONDS: Final[float] = 30.0


class _FailureKind(StrEnum):
    SCHEMA_MISMATCH = "schema_mismatch"
    EVENT_TYPE_MISMATCH = "event_type_mismatch"
    APP_SCOPE_MISMATCH = "app_scope_mismatch"
    TENANT_SCOPE_MISMATCH = "tenant_scope_mismatch"
    SENDER_TYPE_UNSUPPORTED = "sender_type_unsupported"
    MESSAGE_TYPE_UNSUPPORTED = "message_type_unsupported"
    CHAT_TYPE_UNSUPPORTED = "chat_type_unsupported"
    BOT_MENTION_MISSING = "bot_mention_missing"
    MESSAGE_EMPTY = "message_empty"
    IDENTITY_UNMAPPED = "identity_unmapped"
    SUBMIT_FORBIDDEN = "submit_forbidden"
    SUBMISSION_CONFLICT = "submission_conflict"
    CONFIGURATION_INVALID = "configuration_invalid"
    CALLBACK_TIMEOUT = "callback_timeout"
    LISTENER_FAILURE = "listener_failure"


_EXPECTED_FILTERS: Final[frozenset[_FailureKind]] = frozenset(
    {
        _FailureKind.SENDER_TYPE_UNSUPPORTED,
        _FailureKind.MESSAGE_TYPE_UNSUPPORTED,
        _FailureKind.CHAT_TYPE_UNSUPPORTED,
        _FailureKind.BOT_MENTION_MISSING,
        _FailureKind.MESSAGE_EMPTY,
    }
)


class _ListenerProcessStack(Protocol):
    @property
    def listener(self) -> "FeishuListener": ...

    @property
    def transport(self) -> FeishuInboundTransport: ...

    @property
    def aclose(self) -> Callable[[], Awaitable[None]]: ...


class FeishuListener:
    """验证消息协议并把服务端身份上下文交给统一提交服务。"""

    def __init__(
        self,
        *,
        app_id: str,
        tenant_key: str,
        bot_open_id: str,
        tenant_id: str,
        environment_id: str,
        identity_directory: FeishuIdentityDirectory,
        submission_service: ChannelSubmissionService,
        activation_service: IdentityActivationService,
        activation_notifications: ActivationNotificationService,
        policy_revision: str,
        clock: Callable[[], dt.datetime],
        trace_id_factory: Callable[[], str] = new_trace_id,
    ) -> None:
        self._app_id = app_id
        self._tenant_key = tenant_key
        self._bot_open_id = bot_open_id
        self._tenant_id = tenant_id
        self._environment_id = environment_id
        self._identities = identity_directory
        self._submissions = submission_service
        self._activations = activation_service
        self._activation_notifications = activation_notifications
        self._policy_revision = policy_revision
        self._clock = clock
        self._trace_id_factory = trace_id_factory

    def _reject(self, failure_kind: _FailureKind) -> bool:
        """记录服务端作用域与闭集原因；诊断故障不改变 ACK 语义。"""
        level = logging.INFO if failure_kind in _EXPECTED_FILTERS else logging.WARNING
        try:
            _LOGGER.log(
                level,
                "feishu event rejected",
                extra={
                    "tenant_id": self._tenant_id,
                    "environment_id": self._environment_id,
                    "failure_kind": failure_kind.value,
                },
            )
        except Exception:
            return False
        return False

    def _submission_text(
        self, event: FeishuMessageEvent
    ) -> tuple[ChannelKind, str] | _FailureKind:
        if event.message_type != "text":
            return _FailureKind.MESSAGE_TYPE_UNSUPPORTED
        if event.text is None:
            return _FailureKind.MESSAGE_EMPTY
        if event.chat_type == "p2p":
            if not event.text.strip():
                return _FailureKind.MESSAGE_EMPTY
            return ChannelKind.FEISHU_PRIVATE, event.text
        if event.chat_type != "group":
            return _FailureKind.CHAT_TYPE_UNSUPPORTED
        bot_keys = tuple(
            dict.fromkeys(
                mention.key
                for mention in event.mentions
                if mention.subject_ref == self._bot_open_id
            )
        )
        present_bot_keys = tuple(key for key in bot_keys if key in event.text)
        if not present_bot_keys:
            return _FailureKind.BOT_MENTION_MISSING
        text = event.text
        for key in present_bot_keys:
            text = text.replace(key, "", 1)
        if not text.strip():
            return _FailureKind.MESSAGE_EMPTY
        return ChannelKind.FEISHU_GROUP, text

    async def handle_event(self, *, event: FeishuMessageEvent) -> bool:
        """持久化或幂等确认后返回真；永久拒绝返回假并让 SDK 正常 ACK。"""
        if event.event_schema != "2.0":
            return self._reject(_FailureKind.SCHEMA_MISMATCH)
        if event.event_type != _EVENT_TYPE:
            return self._reject(_FailureKind.EVENT_TYPE_MISMATCH)
        if event.app_id != self._app_id:
            return self._reject(_FailureKind.APP_SCOPE_MISMATCH)
        if event.tenant_key != self._tenant_key:
            return self._reject(_FailureKind.TENANT_SCOPE_MISMATCH)
        if event.sender_type != "user":
            return self._reject(_FailureKind.SENDER_TYPE_UNSUPPORTED)
        submission = self._submission_text(event)
        if isinstance(submission, _FailureKind):
            return self._reject(submission)
        channel, text = submission
        try:
            principal = await self._identities.resolve(
                subject_ref=event.sender_subject_ref
            )
        except FeishuIdentityUnavailableError:
            return self._reject(_FailureKind.IDENTITY_UNMAPPED)
        except FeishuIdentityNotFoundError:
            if channel is ChannelKind.FEISHU_PRIVATE:
                return self._reject(_FailureKind.IDENTITY_UNMAPPED)
            submitted = True
            try:
                await self._activations.request_group(
                    subject_ref=event.sender_subject_ref,
                    event_ref=event.event_id,
                    chat_ref=event.chat_id,
                )
            except ActivationCapacityError:
                submitted = False
            await self._activation_notifications.notify(
                conversation_ref=event.chat_id,
                event_id=event.event_id,
                submitted=submitted,
            )
            return True
        trace_id = self._trace_id_factory()
        try:
            with bind_trace_id(trace_id):
                await self._submissions.submit(
                    command=ChannelSubmitCommand(
                        principal=principal,
                        channel=channel,
                        request_id=f"feishu:{event.event_id}",
                        trace_id=trace_id,
                        policy_revision=self._policy_revision,
                        text=text,
                        client_submission_ref=event.event_id,
                        conversation_ref=(
                            event.chat_id
                            if channel is ChannelKind.FEISHU_GROUP
                            else None
                        ),
                        private_chat_ref=(
                            event.chat_id
                            if channel is ChannelKind.FEISHU_PRIVATE
                            else None
                        ),
                        submitted_at=self._clock(),
                    )
                )
        except ChannelSubmissionForbiddenError:
            return self._reject(_FailureKind.SUBMIT_FORBIDDEN)
        except (
            IdempotencyConflictError,
            ChannelBindingConflictError,
            ProjectionSubscriptionConflictError,
        ):
            return self._reject(_FailureKind.SUBMISSION_CONFLICT)
        return True


def _consume_callback_failure(future: Future[bool]) -> None:
    """取走超时后的最终异常，避免无人观察的 Future 告警。"""
    try:
        future.exception()
    except FutureCancelledError:
        return


def _record_process_failure(failure_kind: _FailureKind) -> None:
    """记录不含配置取值或供应商错误的进程级闭集诊断。"""
    message = {
        _FailureKind.CONFIGURATION_INVALID: "feishu listener configuration invalid",
        _FailureKind.CALLBACK_TIMEOUT: "feishu listener callback timed out",
        _FailureKind.LISTENER_FAILURE: "feishu listener stopped",
    }[failure_kind]
    try:
        _LOGGER.log(
            logging.ERROR,
            message,
            extra={"failure_kind": failure_kind.value},
        )
    except Exception:
        return


async def serve_listener(*, stack: _ListenerProcessStack) -> int:
    """把 SDK 同步 callback 桥接到主 asyncio loop，持久化完成后才返回 ACK。"""
    loop = asyncio.get_running_loop()
    callback_lock = Lock()

    def on_event(event: FeishuMessageEvent) -> None:
        # 单进程只允许一个在途 callback；扩容应增加进程，而不是无界积压协程。
        with callback_lock:
            future = asyncio.run_coroutine_threadsafe(
                stack.listener.handle_event(event=event), loop
            )
            try:
                future.result(timeout=_CALLBACK_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                future.add_done_callback(_consume_callback_failure)
                future.cancel()
                _record_process_failure(_FailureKind.CALLBACK_TIMEOUT)
                raise

    try:
        await asyncio.to_thread(stack.transport.run_forever, on_event=on_event)
        return 0
    finally:
        await stack.aclose()


async def _run(settings: Settings) -> int:
    from xiaowei_agent.interfaces.local_stack import (
        build_postgres_feishu_listener_stack,
    )

    try:
        stack = await build_postgres_feishu_listener_stack(settings=settings)
        # 加载回执在**开始服务之前**落库：管理面第一次打开就必须看到这个进程实际
        # 加载的代次，而不是"还没人来问过所以显示待应用"。回执的**归属**由装配函数
        # 按本进程装配了哪条链路算出（见 ``_resolved_credentials``），入口只负责在
        # 真正开始服务之前把它落库——装配成功但没起来的进程不该留下"我在跑第 N 代"。
        await stack.provider_state.record_load(receipts=stack.load_receipts)
        return await serve_listener(stack=stack)
    except Exception:
        # SDK/provider 异常可能携带 URL、ticket 或响应正文，入口只记闭集诊断。
        _record_process_failure(_FailureKind.LISTENER_FAILURE)
        return 1


def main() -> int:
    """加载默认关闭的 listener 配置；不提供 console-script 旁路。"""
    try:
        settings = load_settings()
    except ConfigError:
        _record_process_failure(_FailureKind.CONFIGURATION_INVALID)
        return 2
    if not settings.feishu_listener_enabled:
        return 2
    configure_logging(settings)
    return asyncio.run(_run(settings))


if __name__ == "__main__":  # pragma: no cover - 由进程/Compose 调用
    sys.exit(main())


__all__ = ["FeishuListener", "main", "serve_listener"]

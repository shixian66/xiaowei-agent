"""唯一允许加载 ``lark_oapi`` 的 typed SDK seam。"""

import importlib
import json
import logging
import os
import stat
from collections.abc import Callable
from typing import Final, Protocol, cast

from pydantic import Field, ValidationError

from xiaowei_agent.contracts import Contract, FreeText, StrictStr

_LOGGER = logging.getLogger(__name__)
_SDK_LOGGER_NAME: Final[str] = "Lark"
_MAX_SECRET_BYTES: Final[int] = 4096
_MAX_CONTENT_BYTES: Final[int] = 32_768
_MEMBERS_PAGE_SIZE: Final[int] = 100
_MAX_MEMBER_PAGES: Final[int] = 100
_API_TIMEOUT_SECONDS: Final[float] = 5.0


class FeishuSdkError(RuntimeError):
    """SDK 配置、响应或分页状态无法安全确认。"""


class FeishuMention(Contract):
    """消息正文中的一个 SDK 已解析 mention。"""

    key: StrictStr = Field(max_length=128)
    subject_ref: StrictStr = Field(max_length=256)


class FeishuMessageEvent(Contract):
    """SDK 事件立即收窄后的本地严格 DTO。"""

    event_schema: StrictStr = Field(alias="schema", max_length=16)
    event_id: StrictStr = Field(max_length=256)
    event_type: StrictStr = Field(max_length=128)
    app_id: StrictStr = Field(max_length=256)
    tenant_key: StrictStr = Field(max_length=256)
    sender_type: StrictStr = Field(max_length=32)
    sender_subject_ref: StrictStr = Field(max_length=256)
    message_id: StrictStr = Field(max_length=256)
    chat_id: StrictStr = Field(max_length=256)
    chat_type: StrictStr = Field(max_length=32)
    message_type: StrictStr = Field(max_length=32)
    text: FreeText | None = Field(default=None, max_length=8192)
    mentions: tuple[FeishuMention, ...] = Field(default=(), max_length=50)


FeishuEventHandler = Callable[[FeishuMessageEvent], None]


class FeishuInboundTransport(Protocol):
    """同步长连接 transport；callback 返回后 SDK 才确认事件。"""

    def run_forever(self, *, on_event: FeishuEventHandler) -> None:
        """在当前线程拥有 SDK 事件循环并持续分发本地 DTO。"""


class _AsyncMemberApi(Protocol):
    async def aget(self, request: object) -> object:
        """调用 SDK 的异步群成员分页接口。"""


def _load_lark_oapi() -> object:
    return importlib.import_module("lark_oapi")


def _disable_lark_logging() -> None:
    """SDK 可能记录连接 URL、token 或响应正文，所有调用面统一禁用。"""
    logging.getLogger(_SDK_LOGGER_NAME).disabled = True


def _required_attr(value: object, name: str) -> object:
    candidate = getattr(value, name, None)
    if candidate is None:
        raise FeishuSdkError("feishu sdk payload invalid")
    return candidate


def _required_str(value: object, name: str) -> str:
    candidate = _required_attr(value, name)
    if not isinstance(candidate, str) or not candidate:
        raise FeishuSdkError("feishu sdk payload invalid")
    return candidate


def _parse_text_content(message: object, *, message_type: str) -> str | None:
    if message_type != "text":
        return None
    content = _required_str(message, "content")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise FeishuSdkError("feishu sdk payload invalid")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        raise FeishuSdkError("feishu sdk payload invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"text"}
        or not isinstance(payload["text"], str)
    ):
        raise FeishuSdkError("feishu sdk payload invalid")
    return payload["text"]


def _convert_message_event(raw: object) -> FeishuMessageEvent:
    try:
        header = _required_attr(raw, "header")
        event = _required_attr(raw, "event")
        sender = _required_attr(event, "sender")
        message = _required_attr(event, "message")
        header_tenant = _required_str(header, "tenant_key")
        if _required_str(sender, "tenant_key") != header_tenant:
            raise FeishuSdkError("feishu sdk payload invalid")
        sender_id = _required_attr(sender, "sender_id")
        message_type = _required_str(message, "message_type")
        raw_mentions = getattr(message, "mentions", None)
        if raw_mentions is None:
            raw_mentions = ()
        if not isinstance(raw_mentions, list | tuple) or len(raw_mentions) > 50:
            raise FeishuSdkError("feishu sdk payload invalid")
        mentions = tuple(
            FeishuMention(
                key=_required_str(mention, "key"),
                subject_ref=_required_str(_required_attr(mention, "id"), "open_id"),
            )
            for mention in raw_mentions
        )
        return FeishuMessageEvent(
            schema=_required_str(raw, "schema"),
            event_id=_required_str(header, "event_id"),
            event_type=_required_str(header, "event_type"),
            app_id=_required_str(header, "app_id"),
            tenant_key=header_tenant,
            sender_type=_required_str(sender, "sender_type"),
            sender_subject_ref=_required_str(sender_id, "open_id"),
            message_id=_required_str(message, "message_id"),
            chat_id=_required_str(message, "chat_id"),
            chat_type=_required_str(message, "chat_type"),
            message_type=message_type,
            text=_parse_text_content(message, message_type=message_type),
            mentions=mentions,
        )
    except (AttributeError, TypeError, ValidationError):
        raise FeishuSdkError("feishu sdk payload invalid") from None


def _read_secret_file(path: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("not a regular file")
        payload = os.read(descriptor, _MAX_SECRET_BYTES + 1)
    except (OSError, ValueError):
        raise FeishuSdkError("feishu sdk unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not payload or len(payload) > _MAX_SECRET_BYTES:
        raise FeishuSdkError("feishu sdk unavailable")
    try:
        secret = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise FeishuSdkError("feishu sdk unavailable") from None
    if secret.endswith("\n"):
        secret = secret[:-1]
    if not secret or any(character in secret for character in ("\n", "\r", "\x00")):
        raise FeishuSdkError("feishu sdk unavailable")
    return secret


def _callable_attr(value: object, name: str) -> Callable[..., object]:
    candidate = _required_attr(value, name)
    if not callable(candidate):
        raise FeishuSdkError("feishu sdk unavailable")
    return cast(Callable[..., object], candidate)


class FeishuSdkInboundTransport:
    """运行时才读 credential 并加载 SDK 的长连接实现。"""

    def __init__(self, *, app_id: str, app_secret_file: str) -> None:
        self._app_id = app_id
        self._app_secret_file = app_secret_file

    def run_forever(self, *, on_event: FeishuEventHandler) -> None:
        """在调用线程创建 SDK；无效事件 ACK 后丢弃，应用异常则交 SDK 重试。"""
        failure: FeishuSdkError
        try:
            self._run_sdk(on_event=on_event)
            return
        except Exception:
            # SDK 异常可能携带连接 URL、ticket 或响应正文；不保留异常链。
            failure = FeishuSdkError("feishu sdk unavailable")
        raise failure

    def _run_sdk(self, *, on_event: FeishuEventHandler) -> None:
        _disable_lark_logging()
        secret = _read_secret_file(self._app_secret_file)
        sdk = _load_lark_oapi()

        def dispatch(raw: object) -> None:
            try:
                event = _convert_message_event(raw)
            except FeishuSdkError:
                _LOGGER.warning(
                    "feishu provider event rejected",
                    extra={"failure_kind": "invalid_provider_event"},
                )
                return
            on_event(event)

        dispatcher_class = _required_attr(sdk, "EventDispatcherHandler")
        builder = _callable_attr(dispatcher_class, "builder")("", "")
        builder = _callable_attr(
            builder, "register_p2_im_message_receive_v1"
        )(dispatch)
        dispatcher = _callable_attr(builder, "build")()
        ws = _required_attr(sdk, "ws")
        client = _callable_attr(ws, "Client")(
            self._app_id,
            secret,
            event_handler=dispatcher,
        )
        _callable_attr(client, "start")()


def _build_api_client(*, app_id: str, secret: str) -> _AsyncMemberApi:
    _disable_lark_logging()
    sdk = _load_lark_oapi()
    builder = _callable_attr(_required_attr(sdk, "Client"), "builder")()
    builder = _callable_attr(builder, "app_id")(app_id)
    builder = _callable_attr(builder, "app_secret")(secret)
    builder = _callable_attr(builder, "timeout")(_API_TIMEOUT_SECONDS)
    client = _callable_attr(builder, "build")()
    resource = _required_attr(
        _required_attr(_required_attr(client, "im"), "v1"), "chat_members"
    )
    return cast(_AsyncMemberApi, resource)


def _build_chat_members_request(
    *, chat_id: str, page_size: int, page_token: str | None
) -> object:
    model = importlib.import_module(
        "lark_oapi.api.im.v1.model.get_chat_members_request"
    )
    builder = _callable_attr(_required_attr(model, "GetChatMembersRequest"), "builder")()
    builder = _callable_attr(builder, "member_id_type")("open_id")
    builder = _callable_attr(builder, "page_size")(page_size)
    builder = _callable_attr(builder, "chat_id")(chat_id)
    if page_token is not None:
        builder = _callable_attr(builder, "page_token")(page_token)
    return _callable_attr(builder, "build")()


class FeishuSdkMembershipAdapter:
    """用 ``open_id`` 有界分页确认任意主体当前仍在群内。"""

    def __init__(self, *, tenant_id: str, app_id: str, app_secret_file: str) -> None:
        self._tenant_id = tenant_id
        self._app_id = app_id
        self._app_secret_file = app_secret_file
        self._api: _AsyncMemberApi | None = None

    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        """完整、无歧义地扫完成员页才返回否；任何不确定性抛安全错误。"""
        if tenant_id != self._tenant_id:
            raise FeishuSdkError("feishu membership unavailable")
        failure: FeishuSdkError
        try:
            return await self._check_membership(
                conversation_ref=conversation_ref,
                subject_ref=subject_ref,
            )
        except Exception:
            # 供应商异常可携带原始响应；在 except 块外重建，避免保留异常链。
            failure = FeishuSdkError("feishu membership unavailable")
        raise failure

    async def _check_membership(
        self, *, conversation_ref: str, subject_ref: str
    ) -> bool:
        if self._api is None:
            self._api = _build_api_client(
                app_id=self._app_id,
                secret=_read_secret_file(self._app_secret_file),
            )
        seen_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(_MAX_MEMBER_PAGES):
            request = _build_chat_members_request(
                chat_id=conversation_ref,
                page_size=_MEMBERS_PAGE_SIZE,
                page_token=page_token,
            )
            response = await self._api.aget(request)
            success = _callable_attr(response, "success")()
            if success is not True:
                raise FeishuSdkError("feishu membership unavailable")
            data = _required_attr(response, "data")
            limited = getattr(data, "trigger_security_conf_limit", None)
            has_more = getattr(data, "has_more", None)
            if (
                not isinstance(limited, bool)
                or limited
                or not isinstance(has_more, bool)
            ):
                raise FeishuSdkError("feishu membership unavailable")
            items = getattr(data, "items", None)
            if not isinstance(items, list | tuple) or len(items) > _MEMBERS_PAGE_SIZE:
                raise FeishuSdkError("feishu membership unavailable")
            member_ids = tuple(_required_str(item, "member_id") for item in items)
            if subject_ref in member_ids:
                return True
            if not has_more:
                return False
            next_token = getattr(data, "page_token", None)
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token in seen_tokens
            ):
                raise FeishuSdkError("feishu membership unavailable")
            seen_tokens.add(next_token)
            page_token = next_token
        raise FeishuSdkError("feishu membership unavailable")


__all__ = [
    "FeishuEventHandler",
    "FeishuInboundTransport",
    "FeishuMention",
    "FeishuMessageEvent",
    "FeishuSdkError",
    "FeishuSdkInboundTransport",
    "FeishuSdkMembershipAdapter",
]

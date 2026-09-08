"""真实 SDK 被压缩在一个 typed seam 内，测试不建立任何网络连接。"""

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from xiaowei_agent.interfaces import feishu_sdk
from xiaowei_agent.interfaces.feishu_sdk import (
    FeishuMessageEvent,
    FeishuSdkError,
    FeishuSdkInboundTransport,
    FeishuSdkMembershipAdapter,
)

_NEXT_PAGE = "next"
_REPEATED_PAGE = "same"
_FAKE_SECRET = "unit-test-" + "secret"


def _raw_event() -> SimpleNamespace:
    return SimpleNamespace(
        schema="2.0",
        header=SimpleNamespace(
            event_id="event-1",
            event_type="im.message.receive_v1",
            app_id="cli_test_app",
            tenant_key="tenant-test",
        ),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                tenant_key="tenant-test",
                sender_id=SimpleNamespace(open_id="user-open-id"),
            ),
            message=SimpleNamespace(
                message_id="message-1",
                chat_id="chat-1",
                chat_type="group",
                message_type="text",
                content='{"text":"@_user_1 inspect"}',
                mentions=[
                    SimpleNamespace(
                        key="@_user_1", id=SimpleNamespace(open_id="bot-open-id")
                    )
                ],
            ),
        ),
    )


class _DispatcherBuilder:
    callback: Callable[[object], None] | None = None

    def register_p2_im_message_receive_v1(
        self, callback: Callable[[object], None]
    ) -> "_DispatcherBuilder":
        self.callback = callback
        return self

    def build(self) -> "_DispatcherBuilder":
        return self


class _Dispatcher:
    builder_instance = _DispatcherBuilder()

    @classmethod
    def builder(cls, _encrypt: str, _token: str) -> _DispatcherBuilder:
        cls.builder_instance = _DispatcherBuilder()
        return cls.builder_instance


class _WsClient:
    created: tuple[str, str, object] | None = None

    def __init__(self, app_id: str, secret: str, *, event_handler: object) -> None:
        self.created = (app_id, secret, event_handler)
        type(self).created = self.created

    def start(self) -> None:
        callback = _Dispatcher.builder_instance.callback
        assert callback is not None
        callback(_raw_event())


def test_transport_is_lazy_and_converts_sdk_objects_before_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET + "\n", encoding="utf-8")
    fake_sdk = SimpleNamespace(
        EventDispatcherHandler=_Dispatcher,
        ws=SimpleNamespace(Client=_WsClient),
    )
    loads = 0

    def load() -> object:
        nonlocal loads
        loads += 1
        return fake_sdk

    monkeypatch.setattr(feishu_sdk, "_load_lark_oapi", load)
    transport = FeishuSdkInboundTransport(
        app_id="cli_test_app", app_secret_file=str(secret_file)
    )
    received: list[FeishuMessageEvent] = []
    assert loads == 0

    transport.run_forever(on_event=received.append)

    assert loads == 1
    assert _WsClient.created is not None
    assert _WsClient.created[:2] == ("cli_test_app", _FAKE_SECRET)
    assert received == [
        FeishuMessageEvent(
            schema="2.0",
            event_id="event-1",
            event_type="im.message.receive_v1",
            app_id="cli_test_app",
            tenant_key="tenant-test",
            sender_type="user",
            sender_subject_ref="user-open-id",
            message_id="message-1",
            chat_id="chat-1",
            chat_type="group",
            message_type="text",
            text="@_user_1 inspect",
            mentions=(
                feishu_sdk.FeishuMention(
                    key="@_user_1", subject_ref="bot-open-id"
                ),
            ),
        )
    ]


def test_api_client_builder_sets_the_reviewed_five_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    member_api = _MemberApi([])

    class Builder:
        def app_id(self, value: str) -> "Builder":
            calls.append(("app_id", value))
            return self

        def app_secret(self, value: str) -> "Builder":
            calls.append(("app_secret", value))
            return self

        def timeout(self, value: float) -> "Builder":
            calls.append(("timeout", value))
            return self

        def build(self) -> object:
            return SimpleNamespace(
                im=SimpleNamespace(
                    v1=SimpleNamespace(chat_members=member_api)
                )
            )

    builder = Builder()
    client_class = SimpleNamespace(builder=lambda: builder)
    monkeypatch.setattr(
        feishu_sdk,
        "_load_lark_oapi",
        lambda: SimpleNamespace(Client=client_class),
    )

    assert feishu_sdk._build_api_client(
        app_id="cli_test_app", secret=_FAKE_SECRET
    ) is member_api
    assert calls == [
        ("app_id", "cli_test_app"),
        ("app_secret", _FAKE_SECRET),
        ("timeout", 5.0),
    ]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: setattr(raw.event.sender, "tenant_key", "another-tenant"),
        lambda raw: setattr(raw.event.message, "content", '{"text": 3}'),
        lambda raw: setattr(raw.event.message, "content", '{"text":"ok","x":1}'),
        lambda raw: setattr(raw.event.message, "content", '{"text":"\\ud800"}'),
        lambda raw: setattr(raw.event.message, "mentions", ""),
    ],
)
def test_malformed_sdk_event_is_dropped_without_reaching_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Callable[[SimpleNamespace], None],
) -> None:
    raw = _raw_event()
    mutator(raw)

    class DroppingClient(_WsClient):
        def start(self) -> None:
            callback = _Dispatcher.builder_instance.callback
            assert callback is not None
            callback(raw)

    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    monkeypatch.setattr(
        feishu_sdk,
        "_load_lark_oapi",
        lambda: SimpleNamespace(
            EventDispatcherHandler=_Dispatcher,
            ws=SimpleNamespace(Client=DroppingClient),
        ),
    )
    received: list[FeishuMessageEvent] = []

    FeishuSdkInboundTransport(
        app_id="cli_test_app", app_secret_file=str(secret_file)
    ).run_forever(on_event=received.append)

    assert received == []


class _MemberApi:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    async def aget(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


def _member_response(
    *,
    ids: tuple[str, ...] = (),
    has_more: bool = False,
    page_token: str | None = None,
    success: bool = True,
    limited: bool = False,
) -> object:
    return SimpleNamespace(
        success=lambda: success,
        data=SimpleNamespace(
            items=[SimpleNamespace(member_id=item) for item in ids],
            has_more=has_more,
            page_token=page_token,
            trigger_security_conf_limit=limited,
        ),
    )


def _install_member_fakes(
    monkeypatch: pytest.MonkeyPatch,
    api: _MemberApi,
) -> list[tuple[str, int, str | None]]:
    built: list[tuple[str, int, str | None]] = []
    monkeypatch.setattr(feishu_sdk, "_build_api_client", lambda **_: api)

    def request(*, chat_id: str, page_size: int, page_token: str | None) -> object:
        built.append((chat_id, page_size, page_token))
        return object()

    monkeypatch.setattr(feishu_sdk, "_build_chat_members_request", request)
    return built


async def test_membership_uses_bounded_open_id_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    api = _MemberApi(
        [
            _member_response(ids=("other",), has_more=True, page_token=_NEXT_PAGE),
            _member_response(ids=("subject",)),
        ]
    )
    built = _install_member_fakes(monkeypatch, api)
    adapter = FeishuSdkMembershipAdapter(
        tenant_id="dev-local",
        app_id="cli_test_app",
        app_secret_file=str(secret_file),
    )

    assert await adapter.is_current_group_member(
        tenant_id="dev-local", conversation_ref="chat-1", subject_ref="subject"
    ) is True
    assert built == [("chat-1", 100, None), ("chat-1", 100, _NEXT_PAGE)]
    assert len(api.requests) == 2


async def test_membership_complete_empty_page_is_a_confirmed_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    _install_member_fakes(monkeypatch, _MemberApi([_member_response()]))
    adapter = FeishuSdkMembershipAdapter(
        tenant_id="dev-local",
        app_id="cli_test_app",
        app_secret_file=str(secret_file),
    )

    assert (
        await adapter.is_current_group_member(
            tenant_id="dev-local",
            conversation_ref="chat-1",
            subject_ref="subject",
        )
        is False
    )


@pytest.mark.parametrize(
    "responses",
    [
        [_member_response(success=False)],
        [_member_response(limited=True)],
        [_member_response(has_more=True, page_token=None)],
        [
            _member_response(has_more=True, page_token=_REPEATED_PAGE),
            _member_response(has_more=True, page_token=_REPEATED_PAGE),
        ],
    ],
    ids=["provider-error", "visibility-limit", "missing-token", "repeated-token"],
)
async def test_membership_uncertainty_raises_one_safe_adapter_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[object],
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    _install_member_fakes(monkeypatch, _MemberApi(list(responses)))
    adapter = FeishuSdkMembershipAdapter(
        tenant_id="dev-local",
        app_id="cli_test_app",
        app_secret_file=str(secret_file),
    )

    with pytest.raises(FeishuSdkError, match=r"^feishu membership unavailable$"):
        await adapter.is_current_group_member(
            tenant_id="dev-local", conversation_ref="chat-1", subject_ref="subject"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trigger_security_conf_limit", None),
        ("trigger_security_conf_limit", "false"),
        ("items", None),
        (
            "items",
            [SimpleNamespace(member_id=f"member-{index}") for index in range(101)],
        ),
    ],
    ids=[
        "missing-visibility-flag",
        "invalid-visibility-flag",
        "missing-items",
        "oversized-page",
    ],
)
async def test_membership_malformed_page_cannot_be_treated_as_not_a_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    response = _member_response()
    setattr(response.data, field, value)
    _install_member_fakes(monkeypatch, _MemberApi([response]))
    adapter = FeishuSdkMembershipAdapter(
        tenant_id="dev-local",
        app_id="cli_test_app",
        app_secret_file=str(secret_file),
    )

    with pytest.raises(FeishuSdkError, match=r"^feishu membership unavailable$"):
        await adapter.is_current_group_member(
            tenant_id="dev-local", conversation_ref="chat-1", subject_ref="subject"
        )


async def test_membership_rejects_cross_scope_without_reading_secret(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    adapter = FeishuSdkMembershipAdapter(
        tenant_id="dev-local",
        app_id="cli_test_app",
        app_secret_file=str(missing),
    )

    with pytest.raises(FeishuSdkError, match=r"^feishu membership unavailable$"):
        await adapter.is_current_group_member(
            tenant_id="other", conversation_ref="chat-1", subject_ref="subject"
        )


def test_lark_logger_is_disabled_before_sdk_client_can_log_connection_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    shaped = "wss://example.invalid/connect?ticket=" + "unit-test-" + "ticket"

    class LoggingClient(_WsClient):
        def start(self) -> None:
            logging.getLogger("Lark").info("connected to %s", shaped)

    monkeypatch.setattr(
        feishu_sdk,
        "_load_lark_oapi",
        lambda: SimpleNamespace(
            EventDispatcherHandler=_Dispatcher,
            ws=SimpleNamespace(Client=LoggingClient),
        ),
    )
    logger = logging.getLogger("Lark")
    prior_disabled = logger.disabled
    try:
        with caplog.at_level(logging.DEBUG, logger="Lark"):
            FeishuSdkInboundTransport(
                app_id="cli_test_app", app_secret_file=str(secret_file)
            ).run_forever(on_event=lambda _: None)
        assert shaped not in caplog.text
    finally:
        logger.disabled = prior_disabled


def test_lark_logger_is_disabled_before_http_client_can_log_provider_data(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shaped = "https://open.feishu.invalid?tenant_access_token=" + "unit-test-token"

    class Builder:
        def app_id(self, value: str) -> "Builder":
            del value
            return self

        def app_secret(self, value: str) -> "Builder":
            del value
            return self

        def timeout(self, value: float) -> "Builder":
            del value
            return self

        def build(self) -> object:
            logging.getLogger("Lark").warning("provider request %s", shaped)
            return SimpleNamespace(
                im=SimpleNamespace(
                    v1=SimpleNamespace(chat_members=_MemberApi([]))
                )
            )

    monkeypatch.setattr(
        feishu_sdk,
        "_load_lark_oapi",
        lambda: SimpleNamespace(Client=SimpleNamespace(builder=Builder)),
    )
    logger = logging.getLogger("Lark")
    prior_disabled = logger.disabled
    logger.disabled = False
    try:
        with caplog.at_level(logging.DEBUG, logger="Lark"):
            feishu_sdk._build_api_client(
                app_id="cli_test_app", secret=_FAKE_SECRET
            )
        assert shaped not in caplog.text
    finally:
        logger.disabled = prior_disabled


def test_transport_startup_error_is_generic_and_drops_the_original_context(
    tmp_path: Path,
) -> None:
    transport = FeishuSdkInboundTransport(
        app_id="cli_test_app",
        app_secret_file=str(tmp_path / "missing"),
    )

    with pytest.raises(FeishuSdkError) as caught:
        transport.run_forever(on_event=lambda _: None)

    assert str(caught.value) == "feishu sdk unavailable"
    assert caught.value.__context__ is None


@pytest.mark.parametrize("kind", ["symlink", "multiline", "oversized", "directory"])
def test_transport_rejects_unsafe_secret_files_before_loading_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    secret_file = tmp_path / "secret"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(_FAKE_SECRET, encoding="utf-8")
        secret_file.symlink_to(target)
    elif kind == "multiline":
        secret_file.write_text(_FAKE_SECRET + "\nsecond-line", encoding="utf-8")
    elif kind == "oversized":
        secret_file.write_bytes(b"x" * 4097)
    else:
        secret_file.mkdir()

    def fail_load() -> object:
        raise AssertionError("unsafe secret must fail before SDK import")

    monkeypatch.setattr(feishu_sdk, "_load_lark_oapi", fail_load)
    transport = FeishuSdkInboundTransport(
        app_id="cli_test_app", app_secret_file=str(secret_file)
    )

    with pytest.raises(FeishuSdkError) as caught:
        transport.run_forever(on_event=lambda _: None)

    assert str(caught.value) == "feishu sdk unavailable"
    assert caught.value.__context__ is None

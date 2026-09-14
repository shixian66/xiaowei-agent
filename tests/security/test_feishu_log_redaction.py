"""SDK 与入口异常不得把事件正文、ticket 或供应商错误带入日志。"""

import datetime as dt
import logging
from pathlib import Path
from typing import cast

import pytest

from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
    SubmittedTask,
)
from xiaowei_agent.config import ConfigError, Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
)
from xiaowei_agent.interfaces import feishu_listener
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.feishu_listener import FeishuListener
from xiaowei_agent.interfaces.feishu_sdk import (
    FeishuMessageEvent,
    FeishuSdkError,
    FeishuSdkMembershipAdapter,
)
from xiaowei_agent.persistence.store import IdempotencyConflictError

pytestmark = pytest.mark.security

_FAKE_SECRET = "unit-test-" + "secret"


class _ConflictSubmission:
    def __init__(self, detail: str) -> None:
        self.detail = detail

    async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask:
        del command
        raise IdempotencyConflictError(self.detail)


class _ForbiddenSubmission:
    async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask:
        del command
        raise ChannelSubmissionForbiddenError


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="user-open-id",
        permissions=frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    )


def _event(text: str, **updates: object) -> FeishuMessageEvent:
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
        "text": text,
    }
    return FeishuMessageEvent(**(values | updates))


async def test_submission_conflict_log_contains_no_event_or_provider_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = "query token=" + "unit-test-provider-" + "token"
    provider_detail = "ticket=" + "unit-test-provider-" + "ticket"
    principal = _principal()
    listener = FeishuListener(
        app_id="cli_test_app",
        tenant_key="tenant-test",
        bot_open_id="bot-open-id",
        tenant_id="dev-local",
        environment_id="dev",
        identity_directory=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        submission_service=cast(
            ChannelSubmissionService, _ConflictSubmission(provider_detail)
        ),
        policy_revision="policy-1",
        clock=lambda: dt.datetime(2026, 9, 8, tzinfo=dt.UTC),
        trace_id_factory=lambda: "1" * 32,
    )

    with caplog.at_level(logging.WARNING):
        assert await listener.handle_event(event=_event(body)) is False

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "feishu event rejected"
    ]
    assert len(records) == 1
    assert records[0].failure_kind == "submission_conflict"
    assert records[0].tenant_id == "dev-local"
    assert records[0].environment_id == "dev"
    assert body not in caplog.text
    assert provider_detail not in caplog.text
    assert "event-1" not in caplog.text
    assert "user-open-id" not in caplog.text


@pytest.mark.parametrize(
    ("updates", "failure_kind"),
    [
        ({"schema": "1.0"}, "schema_mismatch"),
        ({"event_type": "other.event"}, "event_type_mismatch"),
        ({"app_id": "external-sensitive-app"}, "app_scope_mismatch"),
        ({"tenant_key": "external-sensitive-tenant"}, "tenant_scope_mismatch"),
        ({"sender_type": "bot"}, "sender_type_unsupported"),
        ({"message_type": "image", "text": None}, "message_type_unsupported"),
        ({"chat_type": "thread"}, "chat_type_unsupported"),
        ({"chat_type": "group"}, "bot_mention_missing"),
        ({"text": "   "}, "message_empty"),
        ({"sender_subject_ref": "external-sensitive-subject"}, "identity_unmapped"),
    ],
)
async def test_rejected_event_emits_one_closed_privacy_safe_diagnostic(
    updates: dict[str, object],
    failure_kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    principal = _principal()
    listener = FeishuListener(
        app_id="cli_test_app",
        tenant_key="tenant-test",
        bot_open_id="bot-open-id",
        tenant_id="dev-local",
        environment_id="dev",
        identity_directory=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        submission_service=cast(
            ChannelSubmissionService, _ConflictSubmission("unused")
        ),
        policy_revision="policy-1",
        clock=lambda: dt.datetime(2026, 9, 8, tzinfo=dt.UTC),
        trace_id_factory=lambda: "1" * 32,
    )

    with caplog.at_level(logging.INFO):
        event = _event(**({"text": "request-body"} | updates))
        assert await listener.handle_event(event=event) is False

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "feishu event rejected"
    ]
    assert len(records) == 1
    assert records[0].failure_kind == failure_kind
    assert records[0].tenant_id == "dev-local"
    assert records[0].environment_id == "dev"
    serialized = f"{records[0].getMessage()} {records[0].__dict__}"
    for forbidden in (
        "request-body",
        "event-1",
        "message-1",
        "chat-1",
        "external-sensitive-app",
        "external-sensitive-tenant",
        "external-sensitive-subject",
    ):
        assert forbidden not in serialized


async def test_forbidden_submission_emits_closed_diagnostic_without_identity_refs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    principal = _principal()
    listener = FeishuListener(
        app_id="cli_test_app",
        tenant_key="tenant-test",
        bot_open_id="bot-open-id",
        tenant_id="dev-local",
        environment_id="dev",
        identity_directory=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        submission_service=cast(ChannelSubmissionService, _ForbiddenSubmission()),
        policy_revision="policy-1",
        clock=lambda: dt.datetime(2026, 9, 8, tzinfo=dt.UTC),
    )

    with caplog.at_level(logging.WARNING):
        assert await listener.handle_event(event=_event("forbidden-body")) is False

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "feishu event rejected"
    ]
    assert len(records) == 1
    assert records[0].failure_kind == "submit_forbidden"
    serialized = f"{records[0].getMessage()} {records[0].__dict__}"
    for forbidden in ("forbidden-body", "event-1", "user-open-id", "chat-1"):
        assert forbidden not in serialized


async def test_event_diagnostic_failure_preserves_ack_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal()
    listener = FeishuListener(
        app_id="cli_test_app",
        tenant_key="tenant-test",
        bot_open_id="bot-open-id",
        tenant_id="dev-local",
        environment_id="dev",
        identity_directory=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        submission_service=cast(
            ChannelSubmissionService, _ConflictSubmission("unused")
        ),
        policy_revision="policy-1",
        clock=lambda: dt.datetime(2026, 9, 8, tzinfo=dt.UTC),
    )

    def fail_logging(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(feishu_listener._LOGGER, "log", fail_logging)

    assert await listener.handle_event(event=_event("ignored", sender_type="bot")) is False


async def test_membership_provider_exception_is_rebuilt_without_response_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(_FAKE_SECRET, encoding="utf-8")
    provider_body = "provider token=" + "unit-test-response-" + "token"

    class FailingApi:
        async def aget(self, request: object) -> object:
            del request
            raise RuntimeError(provider_body)

    from xiaowei_agent.interfaces import feishu_sdk

    monkeypatch.setattr(feishu_sdk, "_build_api_client", lambda **_: FailingApi())
    monkeypatch.setattr(
        feishu_sdk, "_build_chat_members_request", lambda **_: object()
    )
    adapter = FeishuSdkMembershipAdapter(
        tenant_id="dev-local",
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
    )

    with pytest.raises(FeishuSdkError) as caught:
        await adapter.is_current_group_member(
            tenant_id="dev-local", conversation_ref="chat-1", subject_ref="subject"
        )
    assert str(caught.value) == "feishu membership unavailable"
    assert provider_body not in str(caught.value)
    assert caught.value.__context__ is None


async def test_process_boundary_does_not_log_sdk_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from xiaowei_agent.interfaces import local_stack

    provider_body = "connection ticket=" + "unit-test-process-" + "ticket"

    async def fail_builder(*, settings: Settings) -> object:
        del settings
        raise RuntimeError(provider_body)

    monkeypatch.setattr(
        local_stack, "build_postgres_feishu_listener_stack", fail_builder
    )
    settings = Settings(
        environment_id="dev",
        feishu_listener_enabled=True,
        feishu_tenant_key="tenant",
        feishu_bot_open_id="bot",
        feishu_identity_file="/missing/identity-reference",
    )

    with caplog.at_level(logging.ERROR):
        assert await feishu_listener._run(settings) == 1

    assert provider_body not in caplog.text


def test_invalid_configuration_emits_constant_diagnostic_without_error_detail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_detail = "FEISHU_APP_SECRET=" + "unit-test-invalid-" + "secret"

    def fail_settings() -> Settings:
        raise ConfigError(config_detail)

    monkeypatch.setattr(feishu_listener, "load_settings", fail_settings)

    with caplog.at_level(logging.ERROR):
        assert feishu_listener.main() == 2

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "feishu listener configuration invalid"
    ]
    assert len(records) == 1
    assert records[0].failure_kind == "configuration_invalid"
    assert config_detail not in caplog.text

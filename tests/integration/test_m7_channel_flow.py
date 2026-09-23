"""M7 薄渠道经共享 PostgreSQL 与完整 Runtime 的离线纵向闭环。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.fakes.feishu import RecordingFeishuInboundTransport

from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    LOCAL_ADMIN_ACTOR,
    LOCAL_ADMIN_USER_ID,
    ActorTaskPageQuery,
    IdentitySource,
    ProductRole,
    TaskStatus,
    UserStatus,
    WebMode,
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.identity import (
    ApproveActivationCommand,
    RejectActivationCommand,
    SetUserStatusCommand,
)
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces.feishu_sdk import FeishuMention, FeishuMessageEvent
from xiaowei_agent.interfaces.legacy_identity_migration import (
    migrate_static_identities,
)
from xiaowei_agent.interfaces.local_stack import (
    build_postgres_channel_worker_stack,
    build_postgres_feishu_listener_stack,
    build_postgres_local_stack,
    build_postgres_web_stack,
)
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials
from xiaowei_agent.interfaces.web_app import create_app, session_cookie_name
from xiaowei_agent.interfaces.web_auth import (
    FeishuOAuthIdentity,
    WebActivationPendingError,
    WebAuthenticationError,
    WebAuthService,
)
from xiaowei_agent.persistence.postgres import PostgresUserDirectoryStore
from xiaowei_agent.persistence.schema import ACTIVATION_REQUESTS, TASKS, WEB_SESSIONS
from xiaowei_agent.rendering.feishu import RenderedFeishuCard

_SESSION_COOKIE_NAME = session_cookie_name(WebMode.HTTPS)
_WORKBENCH_INTENT = WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)
_I1_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "fixtures"
    / "i1_interaction_cases.json"
)


class _Messages:
    def __init__(self) -> None:
        self.chat_sends: list[tuple[str, RenderedFeishuCard, str]] = []
        self.user_sends: list[tuple[str, RenderedFeishuCard, str]] = []
        self.updates: list[tuple[str, RenderedFeishuCard]] = []
        self._next_ref = 1

    def _message_ref(self) -> str:
        value = f"message-{self._next_ref}"
        self._next_ref += 1
        return value

    async def send_to_chat(
        self,
        *,
        conversation_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        self.chat_sends.append((conversation_ref, card, idempotency_ref))
        return self._message_ref()

    async def send_to_user(
        self,
        *,
        subject_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        self.user_sends.append((subject_ref, card, idempotency_ref))
        return self._message_ref()

    async def update_card(
        self, *, message_ref: str, card: RenderedFeishuCard
    ) -> None:
        self.updates.append((message_ref, card))


class _OAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        assert redirect_uri == "https://ops.example.test/oauth/feishu/callback"
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        assert redirect_uri == "https://ops.example.test/oauth/feishu/callback"
        subjects = {
            "alice-code": "subject-alice",
            "bob-code": "subject-bob",
            "admin-code": "subject-admin",
            "new-user-code": "subject-new-user",
        }
        return FeishuOAuthIdentity(subject_ref=subjects[code])


class _Membership:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        self.calls.append((tenant_id, conversation_ref, subject_ref))
        return True


def _identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "feishu-identities.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": [
                    {
                        "subject_ref": "subject-alice",
                        "actor": "alice",
                        "labels": ["operator"],
                    },
                    {
                        "subject_ref": "subject-admin",
                        "actor": "feishu-admin",
                        "labels": ["admin"],
                    },
                    {
                        "subject_ref": "subject-bob",
                        "actor": "bob",
                        "labels": ["viewer"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment_id="dev",
        feishu_listener_enabled=True,
        channel_worker_enabled=True,
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_tenant_key="offline-tenant",
        feishu_bot_open_id="bot-open-id",
        feishu_identity_file=str(_identity_file(tmp_path)),
        web_public_origin="https://ops.example.test",
    )


def _group_event(
    *,
    text: str = "@_user_1 检查最近三十分钟慢查询",
    event_id: str = "event-group-1",
    message_id: str = "incoming-message-1",
    sender_subject_ref: str = "subject-alice",
) -> FeishuMessageEvent:
    return FeishuMessageEvent(
        schema="2.0",
        event_id=event_id,
        event_type="im.message.receive_v1",
        app_id="offline_test_app",
        tenant_key="offline-tenant",
        sender_type="user",
        sender_subject_ref=sender_subject_ref,
        message_id=message_id,
        chat_id="chat-operations",
        chat_type="group",
        message_type="text",
        text=text,
        mentions=(FeishuMention(key="@_user_1", subject_ref="bot-open-id"),),
    )


def _i1_rejected_text() -> str:
    data = json.loads(_I1_FIXTURE.read_text(encoding="utf-8"))
    case = next(
        item
        for item in data["cases"]
        if item["category"] == "l0_unsupported_runtime"
        and "restricted_select" in item["tags"]
    )
    return str(case["text"])


async def _web_session(
    auth: WebAuthService, *, code: str, return_intent: WebReturnIntent
) -> str:
    started = await auth.start_login(return_intent=return_intent)
    issued = await auth.complete_login(
        code=code,
        state=started.state_cookie,
        state_cookie=started.state_cookie,
        previous_session_cookie=None,
    )
    return issued.session_cookie


async def _migrate_legacy_identities(
    *,
    settings: Settings,
    engine: AsyncEngine,
    clock,
) -> None:
    assert settings.feishu_identity_file is not None
    report = await migrate_static_identities(
        document_path=settings.feishu_identity_file,
        directory=PostgresUserDirectoryStore(engine=engine, clock=clock),
        tenant_id=settings.tenant_id,
        environment_id=settings.environment_id,
        actor_user_id="integration-migration",
        actor="integration-migration",
    )
    assert report.created == 3


async def test_unknown_group_identity_persists_one_request_and_no_task(
    clean_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock,
) -> None:
    settings = _settings(tmp_path)
    messages = _Messages()
    monkeypatch.setattr(
        local_stack_module,
        "create_database_engine",
        lambda _: clean_database,
    )
    listener = await build_postgres_feishu_listener_stack(
        settings=settings,
        clock=clock,
        transport=RecordingFeishuInboundTransport(),
        message_port=messages,
        credentials=ProviderCredentials(
            feishu_app_id="offline_test_app",
            feishu_app_secret="listener-" + "fixture-secret",
        ),
    )
    event = _group_event(
        text="@_user_1 select secret from private_table",
        event_id="event-unknown-group",
        sender_subject_ref="subject-unknown",
    )

    assert await listener.listener.handle_event(event=event) is True
    assert await listener.listener.handle_event(event=event) is True

    async with clean_database.connect() as connection:
        activation_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(ACTIVATION_REQUESTS)
        )
        task_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(TASKS)
        )
    assert activation_count == 1
    assert task_count == 0
    assert len(messages.chat_sends) == 2
    assert messages.chat_sends[0][2] == messages.chat_sends[1][2]
    assert "private_table" not in messages.chat_sends[0][1].content_json
    await listener.aclose()


async def test_bound_but_disabled_identity_never_reenters_activation(
    clean_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock,
) -> None:
    settings = _settings(tmp_path)
    messages = _Messages()
    monkeypatch.setattr(
        local_stack_module,
        "create_database_engine",
        lambda _: clean_database,
    )
    await _migrate_legacy_identities(
        settings=settings, engine=clean_database, clock=clock
    )
    web = await build_postgres_web_stack(
        settings=settings,
        oauth=_OAuth(),
        membership=_Membership(),
        clock=clock,
    )
    listener = await build_postgres_feishu_listener_stack(
        settings=settings,
        clock=clock,
        transport=RecordingFeishuInboundTransport(),
        message_port=messages,
        credentials=ProviderCredentials(
            feishu_app_id="offline_test_app",
            feishu_app_secret="listener-" + "fixture-secret",
        ),
    )
    directory = PostgresUserDirectoryStore(engine=clean_database, clock=clock)
    facts = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=settings.tenant_id,
        environment_id=settings.environment_id,
        subject_ref="subject-alice",
    )
    assert facts is not None
    await directory.apply(
        command=SetUserStatusCommand(
            user_id=facts.account.user_id,
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
            status=UserStatus.DISABLED,
        ),
        context=AdminOperationContext(
            operation_id="integration-disable-alice",
            actor_user_id=LOCAL_ADMIN_USER_ID,
            actor=LOCAL_ADMIN_ACTOR,
            auth_source=IdentitySource.LOCAL_ADMIN,
        ),
    )

    started = await web.auth.start_login(return_intent=_WORKBENCH_INTENT)
    with pytest.raises(WebAuthenticationError):
        await web.auth.complete_login(
            code="alice-code",
            state=started.state_cookie,
            state_cookie=started.state_cookie,
            previous_session_cookie=None,
        )
    assert await listener.listener.handle_event(event=_group_event()) is False

    async with clean_database.connect() as connection:
        activation_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(ACTIVATION_REQUESTS)
        )
        session_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(WEB_SESSIONS)
        )
        task_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(TASKS)
        )
    assert (activation_count, session_count, task_count) == (0, 0, 0)
    assert messages.chat_sends == []
    await listener.aclose()
    await web.aclose()


async def test_feishu_and_web_share_one_runtime_task_truth_and_notification_policy(
    clean_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock,
) -> None:
    settings = _settings(tmp_path)
    messages = _Messages()
    membership = _Membership()
    monkeypatch.setattr(
        local_stack_module,
        "create_database_engine",
        lambda _: clean_database,
    )
    await _migrate_legacy_identities(
        settings=settings, engine=clean_database, clock=clock
    )
    full = await build_postgres_local_stack(
        settings=settings,
        clock=clock,
        monotonic=lambda: 0.0,
    )
    listener = await build_postgres_feishu_listener_stack(
        settings=settings,
        clock=clock,
        transport=RecordingFeishuInboundTransport(),
        message_port=messages,
        credentials=ProviderCredentials(
            feishu_app_id="offline_test_app",
            feishu_app_secret="listener-" + "fixture-secret",
        ),
    )
    projection = await build_postgres_channel_worker_stack(
        settings=settings,
        clock=clock,
        message_port=messages,
        sleep=asyncio.sleep,
    )
    web = await build_postgres_web_stack(
        settings=settings,
        oauth=_OAuth(),
        membership=membership,
        clock=clock,
    )
    worker = WorkerLoop(
        runtime=full.runtime,
        task_store=full.task_store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=settings,
        sleep=asyncio.sleep,
    )
    web_app = create_app(
        auth=web.auth,
        local_admin_auth=web.local_admin_auth,
        oauth_available=web.oauth_available,
        provider_state=web.provider_state,
        settings=settings,
        readiness=web.readiness,
        task_access=web.task_access_service,
        submissions=web.submission_service,
        clock=clock,
        policy_revision=web.policy_revision,
    )

    assert await listener.listener.handle_event(event=_group_event()) is True
    group_page = await listener.task_store.list_tasks_for_actor(
        query=ActorTaskPageQuery(
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
            actor="alice",
            limit=10,
        )
    )
    group_task_id = group_page.items[0].record.task_id

    assert await projection.service.poll_once() == 1
    assert [item[0] for item in messages.chat_sends] == ["chat-operations"]
    assert messages.updates == []
    original_message_ref = "message-1"

    assert await worker.poll_once() == 1
    clock.advance(seconds=2)
    assert await projection.service.poll_once() == 1
    assert [item[0] for item in messages.updates] == [original_message_ref]
    assert "小维处理完成" in messages.updates[0][1].content_json

    bob_cookie = await _web_session(
        web.auth,
        code="bob-code",
        return_intent=WebReturnIntent(
            kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
            task_id=group_task_id,
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={_SESSION_COOKIE_NAME: bob_cookie},
    ) as bob_client:
        group_detail = await bob_client.get(f"/app/api/tasks/{group_task_id}")
        assert group_detail.status_code == 200
        assert group_detail.json()["status"] == TaskStatus.SUCCEEDED.value
        assert group_detail.json()["render"] is not None
        assert membership.calls[-1] == (
            settings.tenant_id,
            "chat-operations",
            "subject-bob",
        )

    alice_cookie = await _web_session(
        web.auth, code="alice-code", return_intent=_WORKBENCH_INTENT
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={_SESSION_COOKIE_NAME: alice_cookie},
    ) as alice_client:

        me = await alice_client.get("/app/api/me")
        normal_submit = await alice_client.post(
            "/app/api/tasks",
            json={
                "text": "检查最近三十分钟慢查询",
                "client_submission_id": "web-normal-submission-1",
            },
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": me.json()["csrf_token"],
            },
        )
    assert normal_submit.status_code == 202
    normal_task_id = normal_submit.json()["task_id"]
    assert await worker.poll_once() == 1
    assert await projection.service.poll_once() == 1
    assert [item[0] for item in messages.user_sends] == ["subject-alice"]
    assert normal_task_id in messages.user_sends[0][1].content_json
    assert await projection.service.poll_once() == 0

    admin_cookie = await _web_session(
        web.auth, code="admin-code", return_intent=_WORKBENCH_INTENT
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={_SESSION_COOKIE_NAME: admin_cookie},
    ) as admin_client:
        me = await admin_client.get("/app/api/me")
        admin_submit = await admin_client.post(
            "/app/api/tasks",
            json={
                "text": "检查最近三十分钟慢查询",
                "client_submission_id": "web-admin-submission-1",
            },
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": me.json()["csrf_token"],
            },
        )
    assert admin_submit.status_code == 202
    assert await worker.poll_once() == 1
    assert await projection.service.poll_once() == 0
    assert [item[0] for item in messages.user_sends] == ["subject-alice"]


async def test_feishu_and_web_submit_same_i1_rejected_case_with_same_projection(
    clean_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock,
) -> None:
    settings = _settings(tmp_path)
    membership = _Membership()
    monkeypatch.setattr(
        local_stack_module,
        "create_database_engine",
        lambda _: clean_database,
    )
    await _migrate_legacy_identities(
        settings=settings, engine=clean_database, clock=clock
    )
    full = await build_postgres_local_stack(
        settings=settings,
        clock=clock,
        monotonic=lambda: 0.0,
    )
    listener = await build_postgres_feishu_listener_stack(
        settings=settings,
        clock=clock,
        transport=RecordingFeishuInboundTransport(),
        message_port=_Messages(),
        credentials=ProviderCredentials(
            feishu_app_id="offline_test_app",
            feishu_app_secret="listener-" + "fixture-secret",
        ),
    )
    web = await build_postgres_web_stack(
        settings=settings,
        oauth=_OAuth(),
        membership=membership,
        clock=clock,
    )
    worker = WorkerLoop(
        runtime=full.runtime,
        task_store=full.task_store,
        clock=clock,
        monotonic=lambda: 0.0,
        settings=settings,
        sleep=asyncio.sleep,
    )
    web_app = create_app(
        auth=web.auth,
        local_admin_auth=web.local_admin_auth,
        oauth_available=web.oauth_available,
        provider_state=web.provider_state,
        settings=settings,
        readiness=web.readiness,
        task_access=web.task_access_service,
        submissions=web.submission_service,
        clock=clock,
        policy_revision=web.policy_revision,
    )
    rejected_text = _i1_rejected_text()

    assert await listener.listener.handle_event(
        event=_group_event(
            text=f"@_user_1 {rejected_text}",
            event_id="event-rejected-feishu",
            message_id="incoming-rejected-feishu",
        )
    ) is True
    group_page = await listener.task_store.list_tasks_for_actor(
        query=ActorTaskPageQuery(
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
            actor="alice",
            limit=10,
        )
    )
    feishu_task_id = group_page.items[0].record.task_id
    assert await worker.poll_once() == 1

    alice_cookie = await _web_session(
        web.auth, code="alice-code", return_intent=_WORKBENCH_INTENT
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={_SESSION_COOKIE_NAME: alice_cookie},
    ) as alice_client:
        feishu_detail = await alice_client.get(f"/app/api/tasks/{feishu_task_id}")
        me = await alice_client.get("/app/api/me")
        web_submit = await alice_client.post(
            "/app/api/tasks",
            json={
                "text": rejected_text,
                "client_submission_id": "web-i1-rejected-case",
            },
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": me.json()["csrf_token"],
            },
        )
        assert web_submit.status_code == 202
        web_task_id = web_submit.json()["task_id"]
        assert await worker.poll_once() == 1
        web_detail = await alice_client.get(f"/app/api/tasks/{web_task_id}")

    assert feishu_detail.status_code == 200
    assert web_detail.status_code == 200
    feishu_json = feishu_detail.json()
    web_json = web_detail.json()
    assert feishu_json["status"] == web_json["status"] == TaskStatus.REJECTED.value
    assert feishu_json["render"] == web_json["render"]
    assert feishu_json.get("disclosure") is None
    assert web_json.get("disclosure") is None


async def test_unknown_oauth_identity_can_log_in_only_after_admin_approval(
    clean_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        local_stack_module,
        "create_database_engine",
        lambda _: clean_database,
    )
    web = await build_postgres_web_stack(
        settings=settings,
        oauth=_OAuth(),
        membership=_Membership(),
        clock=clock,
    )
    assert web.auth is not None
    assert web.activation_service is not None
    original_intent = WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="activation-preview-task",
    )

    first = await web.auth.start_login(return_intent=original_intent)
    with pytest.raises(WebActivationPendingError):
        await web.auth.complete_login(
            code="new-user-code",
            state=first.state_cookie,
            state_cookie=first.state_cookie,
            previous_session_cookie=None,
        )

    async with clean_database.connect() as connection:
        request_id = await connection.scalar(
            sa.select(ACTIVATION_REQUESTS.c.request_id)
        )
        session_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(WEB_SESSIONS)
        )
    assert isinstance(request_id, str)
    assert session_count == 0

    await web.activation_service.decide(
        command=RejectActivationCommand(
            request_id=request_id,
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
        ),
        context=AdminOperationContext(
            operation_id="integration-reject-new-user",
            actor_user_id=LOCAL_ADMIN_USER_ID,
            actor=LOCAL_ADMIN_ACTOR,
            auth_source=IdentitySource.LOCAL_ADMIN,
        ),
    )

    after_rejection = await web.auth.start_login(return_intent=original_intent)
    with pytest.raises(WebActivationPendingError):
        await web.auth.complete_login(
            code="new-user-code",
            state=after_rejection.state_cookie,
            state_cookie=after_rejection.state_cookie,
            previous_session_cookie=None,
        )

    async with clean_database.connect() as connection:
        pending_request_id = await connection.scalar(
            sa.select(ACTIVATION_REQUESTS.c.request_id).where(
                ACTIVATION_REQUESTS.c.status == "pending"
            )
        )
        session_count = await connection.scalar(
            sa.select(sa.func.count()).select_from(WEB_SESSIONS)
        )
    assert isinstance(pending_request_id, str)
    assert pending_request_id != request_id
    assert session_count == 0

    await web.activation_service.decide(
        command=ApproveActivationCommand(
            request_id=pending_request_id,
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
            actor="new-user",
            display_name="New User",
        ),
        context=AdminOperationContext(
            operation_id="integration-approve-new-user",
            actor_user_id=LOCAL_ADMIN_USER_ID,
            actor=LOCAL_ADMIN_ACTOR,
            auth_source=IdentitySource.LOCAL_ADMIN,
        ),
    )

    second = await web.auth.start_login(return_intent=original_intent)
    issued = await web.auth.complete_login(
        code="new-user-code",
        state=second.state_cookie,
        state_cookie=second.state_cookie,
        previous_session_cookie=None,
    )

    assert issued.principal.actor == "new-user"
    assert issued.principal.subject_ref == "subject-new-user"
    assert issued.role is ProductRole.USER
    assert issued.return_intent == original_intent
    await web.aclose()

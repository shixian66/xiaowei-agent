"""M7 薄渠道经共享 PostgreSQL 与完整 Runtime 的离线纵向闭环。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.fakes.feishu import RecordingFeishuInboundTransport

from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ActorTaskPageQuery, TaskStatus
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces.feishu_sdk import FeishuMention, FeishuMessageEvent
from xiaowei_agent.interfaces.local_stack import (
    build_postgres_channel_worker_stack,
    build_postgres_feishu_listener_stack,
    build_postgres_local_stack,
    build_postgres_web_stack,
)
from xiaowei_agent.interfaces.web_app import SESSION_COOKIE_NAME, create_app
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity, WebAuthService
from xiaowei_agent.rendering.feishu import RenderedFeishuCard


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
                        "actor": "admin",
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
        feishu_app_id="offline_test_app",
        feishu_app_secret_file=str(tmp_path / "unused-app-secret"),
        feishu_tenant_key="offline-tenant",
        feishu_bot_open_id="bot-open-id",
        feishu_identity_file=str(_identity_file(tmp_path)),
        web_detail_base_url="https://ops.example.test",
    )


def _group_event() -> FeishuMessageEvent:
    return FeishuMessageEvent(
        schema="2.0",
        event_id="event-group-1",
        event_type="im.message.receive_v1",
        app_id="offline_test_app",
        tenant_key="offline-tenant",
        sender_type="user",
        sender_subject_ref="subject-alice",
        message_id="incoming-message-1",
        chat_id="chat-operations",
        chat_type="group",
        message_type="text",
        text="@_user_1 检查最近三十分钟慢查询",
        mentions=(FeishuMention(key="@_user_1", subject_ref="bot-open-id"),),
    )


async def _web_session(auth: WebAuthService, *, code: str) -> str:
    started = await auth.start_login()
    issued = await auth.complete_login(
        code=code,
        state=started.state_cookie,
        state_cookie=started.state_cookie,
        previous_session_cookie=None,
    )
    return issued.session_cookie


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
    full = await build_postgres_local_stack(
        settings=settings,
        clock=clock,
        monotonic=lambda: 0.0,
    )
    listener = await build_postgres_feishu_listener_stack(
        settings=settings,
        clock=clock,
        transport=RecordingFeishuInboundTransport(),
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

    bob_cookie = await _web_session(web.auth, code="bob-code")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={SESSION_COOKIE_NAME: bob_cookie},
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

    alice_cookie = await _web_session(web.auth, code="alice-code")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={SESSION_COOKIE_NAME: alice_cookie},
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

    admin_cookie = await _web_session(web.auth, code="admin-code")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        cookies={SESSION_COOKIE_NAME: admin_cookie},
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

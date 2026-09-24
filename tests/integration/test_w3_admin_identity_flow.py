"""W3-lite 从身份申请到受 ACL 约束结果链接的离线纵向闭环。"""

import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.application.channel_submission import ChannelSubmitCommand
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    ChannelKind,
    ProductRole,
    WebMode,
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces.legacy_identity_migration import (
    migrate_static_identities,
)
from xiaowei_agent.interfaces.local_admin_auth import (
    INITIAL_LOCAL_ADMIN_PASSWORD,
)
from xiaowei_agent.interfaces.local_stack import build_postgres_web_stack
from xiaowei_agent.interfaces.web_app import create_app, session_cookie_name
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity, web_csrf_token
from xiaowei_agent.persistence.postgres import PostgresUserDirectoryStore
from xiaowei_agent.persistence.schema import TASKS

_ORIGIN = "https://ops.example.test"
_SESSION_COOKIE = session_cookie_name(WebMode.HTTPS)
_NOW = dt.datetime(2026, 9, 24, 10, 30, tzinfo=dt.UTC)


class _OAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        assert redirect_uri == f"{_ORIGIN}/oauth/feishu/callback"
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        assert redirect_uri == f"{_ORIGIN}/oauth/feishu/callback"
        subjects = {
            "alice-code": "subject-alice",
            "admin-code": "subject-admin",
            "new-user-code": "subject-new-user",
        }
        return FeishuOAuthIdentity(subject_ref=subjects[code])


class _Membership:
    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        return (
            tenant_id == "dev-local"
            and conversation_ref == "chat-w3-lite"
            and subject_ref == "subject-new-user"
        )


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
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_identity_file=str(_identity_file(tmp_path)),
        web_public_origin=_ORIGIN,
    )


async def _migrate(
    *, settings: Settings, engine: AsyncEngine, clock
) -> None:
    assert settings.feishu_identity_file is not None
    report = await migrate_static_identities(
        document_path=settings.feishu_identity_file,
        directory=PostgresUserDirectoryStore(engine=engine, clock=clock),
        tenant_id=settings.tenant_id,
        environment_id=settings.environment_id,
        actor_user_id="w3-lite-migration",
        actor="w3-lite-migration",
    )
    assert report.created == 2


def _app(web, settings: Settings):
    return create_app(
        auth=web.auth,
        local_admin_auth=web.local_admin_auth,
        oauth_available=web.oauth_available,
        provider_state=web.provider_state,
        settings=settings,
        readiness=web.readiness,
        task_access=web.task_access_service,
        submissions=web.submission_service,
        clock=web.clock,
        policy_revision=web.policy_revision,
        admin_identity=web.admin_identity_service,
    )


async def _oauth_callback(
    client: httpx.AsyncClient,
    *,
    code: str,
    query: dict[str, str],
) -> httpx.Response:
    started = await client.get("/oauth/feishu/start", params=query)
    assert started.status_code == 302
    states = parse_qs(urlsplit(started.headers["location"]).query).get("state", [])
    assert len(states) == 1
    return await client.get(
        "/oauth/feishu/callback",
        params={"code": code, "state": states[0]},
    )


def _write_headers(csrf_token: str) -> dict[str, str]:
    return {"origin": _ORIGIN, "x-csrf-token": csrf_token}


async def test_oauth_activation_admin_approval_restores_only_the_saved_intent_and_acl(
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
    await _migrate(settings=settings, engine=clean_database, clock=clock)
    web = await build_postgres_web_stack(
        settings=settings,
        oauth=_OAuth(),
        membership=_Membership(),
        clock=clock,
    )
    assert web.auth is not None
    assert web.identity_directory is not None
    app = _app(web, settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=_ORIGIN,
    ) as alice:
        callback = await _oauth_callback(
            alice,
            code="alice-code",
            query={"intent": "workbench"},
        )
        assert callback.status_code == 302
        me = await alice.get("/app/api/me")
        private = await alice.post(
            "/app/api/tasks",
            json={
                "text": "查看 Alice 的只读结果",
                "client_submission_id": "w3-lite-private-task",
            },
            headers=_write_headers(me.json()["csrf_token"]),
        )
        assert private.status_code == 202
        private_task_id = private.json()["task_id"]

    alice_resolution = await web.identity_directory.resolve_for_web(
        subject_ref="subject-alice"
    )
    group = await web.submission_service.submit(
        command=ChannelSubmitCommand(
            principal=alice_resolution.principal,
            channel=ChannelKind.FEISHU_GROUP,
            request_id="w3-lite-group-request",
            trace_id="1" * 32,
            policy_revision=web.policy_revision,
            text="查看群内共享的只读结果",
            client_submission_ref="w3-lite-group-event",
            conversation_ref="chat-w3-lite",
            submitted_at=_NOW,
        )
    )
    group_task_id = group.task_view.task_id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=_ORIGIN,
    ) as applicant:
        pending = await _oauth_callback(
            applicant,
            code="new-user-code",
            query={"intent": "safe_task_detail", "task_id": private_task_id},
        )
        assert pending.status_code == 302
        pending_query = parse_qs(urlsplit(pending.headers["location"]).query)
        assert pending_query["intent"] == ["activation_status"]
        request_id = pending_query["request_id"][0]
        assert _SESSION_COOKIE not in applicant.cookies

        async with clean_database.connect() as connection:
            task_count_before = await connection.scalar(
                sa.select(sa.func.count()).select_from(TASKS)
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=_ORIGIN,
        ) as admin:
            logged_in = await _oauth_callback(
                admin,
                code="admin-code",
                query={"intent": "admin_center"},
            )
            assert logged_in.headers["location"] == "/admin"
            admin_me = await admin.get("/app/api/me")
            pending_page = await admin.get("/admin/api/activations")
            assert pending_page.status_code == 200
            assert [item["request_id"] for item in pending_page.json()["items"]] == [
                request_id
            ]
            approved = await admin.post(
                "/admin/api/activations/approve",
                json={
                    "request_id": request_id,
                    "actor": "new-user",
                    "display_name": "New User",
                    "approved_role": "user",
                    "confirm": True,
                },
                headers=_write_headers(admin_me.json()["csrf_token"]),
            )
            assert approved.status_code == 204
            audit = await admin.get("/admin/api/audit")
            assert any(
                item["action"] == "activation_approved"
                and item["outcome"] == "succeeded"
                for item in audit.json()["items"]
            )

        resumed = await _oauth_callback(
            applicant,
            code="new-user-code",
            query={"intent": "activation_status", "request_id": request_id},
        )
        assert resumed.status_code == 302
        assert resumed.headers["location"] == f"/app/tasks/{private_task_id}"
        assert _SESSION_COOKIE in applicant.cookies
        hidden_private = await applicant.get(f"/app/api/tasks/{private_task_id}")
        assert hidden_private.status_code == 404

        allowed = await _oauth_callback(
            applicant,
            code="new-user-code",
            query={"intent": "safe_task_detail", "task_id": group_task_id},
        )
        assert allowed.status_code == 302
        assert allowed.headers["location"] == f"/app/tasks/{group_task_id}"
        group_detail = await applicant.get(f"/app/api/tasks/{group_task_id}")
        assert group_detail.status_code == 200

        async with clean_database.connect() as connection:
            task_count_after = await connection.scalar(
                sa.select(sa.func.count()).select_from(TASKS)
            )
        assert task_count_after == task_count_before

    await web.aclose()


async def test_local_admin_can_manage_identity_when_oauth_is_not_assembled(
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
        oauth=None,
        membership=None,
        clock=clock,
    )
    assert web.auth is None
    assert web.oauth_available is False
    app = _app(web, settings)
    request = await web.activation_service.request_web(
        subject_ref="subject-offline-user",
        return_intent=WebReturnIntent(
            kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
            task_id="offline-task",
        ),
    )
    new_password = "rotated-local-" + "admin-secret"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=_ORIGIN,
    ) as client:
        assert (await client.get("/oauth/feishu/start")).status_code == 404
        login = await client.post(
            "/login/api/login",
            json={
                "username": "admin",
                "password": INITIAL_LOCAL_ADMIN_PASSWORD,
                "return_intent": {"kind": "admin_center"},
            },
            headers={"origin": _ORIGIN},
        )
        assert login.status_code == 200
        first_cookie = client.cookies.get(_SESSION_COOKIE)
        assert first_cookie is not None
        changed = await client.post(
            "/login/api/change-password",
            json={
                "current_password": INITIAL_LOCAL_ADMIN_PASSWORD,
                "new_password": new_password,
                "return_intent": {"kind": "admin_center"},
            },
            headers=_write_headers(web_csrf_token(first_cookie)),
        )
        assert changed.status_code == 200
        me = await client.get("/app/api/me")
        assert me.status_code == 200
        csrf = me.json()["csrf_token"]
        assert (await client.get("/admin/api/users")).status_code == 200
        assert (await client.get("/admin/api/audit")).status_code == 200
        pending = await client.get("/admin/api/activations")
        assert [item["request_id"] for item in pending.json()["items"]] == [
            request.request_id
        ]
        approved = await client.post(
            "/admin/api/activations/approve",
            json={
                "request_id": request.request_id,
                "actor": "offline-user",
                "display_name": "Offline User",
                "approved_role": ProductRole.USER.value,
                "confirm": True,
            },
            headers=_write_headers(csrf),
        )
        assert approved.status_code == 204

    await web.aclose()

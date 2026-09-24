"""隔离 Web app 的 OAuth、Cookie、安全头与路由闭集。"""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from tests.fakes.web_auth import EmptyProviderState, NoLocalAdmin

from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    IdentitySource,
    ProductRole,
    ReadinessReport,
    WebMode,
)
from xiaowei_agent.governance.product_roles import channel_permissions
from xiaowei_agent.interfaces import feishu_oauth as feishu_oauth_module
from xiaowei_agent.interfaces import feishu_sdk as feishu_sdk_module
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces import provider_consumption as provider_consumption_module
from xiaowei_agent.interfaces import web_app as web_app_module
from xiaowei_agent.interfaces import web_auth as web_auth_module
from xiaowei_agent.interfaces.api import create_app as create_internal_app
from xiaowei_agent.interfaces.feishu_identity import (
    StaticFeishuIdentityDirectory,
    WebIdentityResolution,
)
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials
from xiaowei_agent.interfaces.web_app import (
    create_app,
    oauth_state_cookie_name,
    session_cookie_name,
)
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity, WebAuthService
from xiaowei_agent.persistence.fake import (
    InMemoryActivationStore,
    InMemoryAdminAuditStore,
    InMemoryUserDirectoryStore,
    InMemoryWebSessionStore,
)
from xiaowei_agent.trace import get_trace_id

_SESSION_COOKIE_NAME = session_cookie_name(WebMode.HTTPS)
_OAUTH_STATE_COOKIE_NAME = oauth_state_cookie_name(WebMode.HTTPS)


class _Probe:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready

    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=self.ready,
            revision_matches_head=self.ready,
            assembled=self.ready,
        )


class _OAuth:
    def __init__(
        self,
        subject_ref: str = "subject-alice",
        *,
        public_origin: str = "https://ops.example.test",
    ) -> None:
        self.subject_ref = subject_ref
        self.redirect_uri = f"{public_origin}/oauth/feishu/callback"
        self.states: list[str] = []
        self.exchange_calls = 0
        self.authorization_trace_ids: list[str | None] = []
        self.exchange_trace_ids: list[str | None] = []
        self.identity_trace_ids: list[str | None] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        self.authorization_trace_ids.append(get_trace_id())
        self.states.append(state)
        assert redirect_uri == self.redirect_uri
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        self.exchange_trace_ids.append(get_trace_id())
        assert code == "valid-code"
        assert redirect_uri == self.redirect_uri
        self.exchange_calls += 1
        return FeishuOAuthIdentity(subject_ref=self.subject_ref)


class _TracingIdentityDirectory:
    def __init__(
        self,
        *,
        principal: AuthenticatedPrincipal,
        role: ProductRole,
        trace_ids: list[str | None],
    ) -> None:
        self._delegate = StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal},
            web_roles={principal.subject_ref: role},
            web_user_ids={principal.subject_ref: "user-alice"},
        )
        self._trace_ids = trace_ids

    async def resolve(self, *, subject_ref: str) -> AuthenticatedPrincipal:
        self._trace_ids.append(get_trace_id())
        return await self._delegate.resolve(subject_ref=subject_ref)

    async def resolve_for_web(
        self, *, subject_ref: str
    ) -> WebIdentityResolution:
        self._trace_ids.append(get_trace_id())
        return await self._delegate.resolve_for_web(subject_ref=subject_ref)


def _principal(*, role: ProductRole = ProductRole.OPERATOR) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="subject-alice",
        permissions=channel_permissions(role=role),
    )


def _settings(*, public_origin: str = "https://ops.example.test") -> Settings:
    return Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_identity_file="/run/config/feishu-identities.json",
        web_public_origin=public_origin,
    )


def _tokens() -> Iterator[str]:
    yield from (
        "state_value_first_1234567890",
        "session_value_first_1234567890",
        "state_value_second_1234567890",
        "session_value_second_1234567890",
    )


def _web_app(
    clock,
    memory_state,
    *,
    subject_ref: str = "subject-alice",
    role: ProductRole = ProductRole.OPERATOR,
    public_origin: str = "https://ops.example.test",
) -> Any:
    oauth = _OAuth(subject_ref=subject_ref, public_origin=public_origin)
    token_values = _tokens()
    principal = _principal(role=role)
    auth = WebAuthService(
        sessions=InMemoryWebSessionStore(clock=clock, state=memory_state),
        identities=_TracingIdentityDirectory(
            principal=principal,
            role=role,
            trace_ids=oauth.identity_trace_ids,
        ),
        activations=IdentityActivationService(
            activations=InMemoryActivationStore(clock=clock, state=memory_state),
            directory=InMemoryUserDirectoryStore(clock=clock, state=memory_state),
            audit=InMemoryAdminAuditStore(clock=clock, state=memory_state),
            tenant_id="dev-local",
            environment_id="dev",
        ),
        oauth=oauth,
        public_origin=public_origin,
        mode=WebMode.HTTPS,
        oauth_state_ttl_seconds=300,
        session_ttl_seconds=3600,
        token_factory=lambda: next(token_values),
    )
    class TaskAccess:
        async def list_tasks(self, *, query: object) -> object:
            raise AssertionError(query)

        async def get_task(self, *, query: object) -> object:
            raise AssertionError(query)

    class Submissions:
        async def submit(self, *, command: object) -> object:
            raise AssertionError(command)

    return (
        create_app(
            auth=auth,
            local_admin_auth=NoLocalAdmin(),
            oauth_available=True,
            settings=_settings(public_origin=public_origin),
            readiness=_Probe(),
            task_access=TaskAccess(),
            submissions=Submissions(),
            clock=clock,
            policy_revision="policy-2026-09-01",
            provider_state=EmptyProviderState(),
        ),
        oauth,
    )


def _client(
    app: Any, *, base_url: str = "https://ops.example.test"
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=base_url,
        follow_redirects=False,
    )


async def _raw_get(
    app: Any, *, path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, object], list[tuple[bytes, bytes]]]:
    sent: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 55000),
            "server": ("127.0.0.1", 8080),
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body), list(start["headers"])


async def _login(client: httpx.AsyncClient, oauth: _OAuth) -> httpx.Response:
    start = await client.get("/oauth/feishu/start")
    assert start.status_code == 302
    return await client.get(
        "/oauth/feishu/callback",
        params={"code": "valid-code", "state": oauth.states[-1]},
    )


async def _login_for(
    client: httpx.AsyncClient,
    oauth: _OAuth,
    *,
    intent: str,
    task_id: str | None = None,
) -> httpx.Response:
    params: list[tuple[str, str]] = [("intent", intent)]
    if task_id is not None:
        params.append(("task_id", task_id))
    started = await client.get("/oauth/feishu/start", params=params)
    assert started.status_code == 302
    return await client.get(
        "/oauth/feishu/callback",
        params={"code": "valid-code", "state": oauth.states[-1]},
    )


async def test_real_protected_routes_return_401_unauthorized(clock, memory_state) -> None:
    app, _ = _web_app(clock, memory_state)
    async with _client(app) as client:
        for path in (
            "/app/api/me",
            "/app/api/tasks",
            "/app/api/tasks/task-1",
        ):
            response = await client.get(path)
            assert response.status_code == 401
            assert response.json() == {"error": {"code": "unauthorized"}}


async def test_anonymous_shells_redirect_to_the_independent_login_page(
    clock, memory_state
) -> None:
    app, _ = _web_app(clock, memory_state)
    async with _client(app) as client:
        workbench = await client.get("/app")
        detail = await client.get("/app/tasks/task-1")
        admin = await client.get("/admin")
        response = await client.get("/login?intent=workbench")

    assert workbench.status_code == 302
    assert workbench.headers["location"] == "/login?intent=workbench"
    assert detail.status_code == 302
    assert detail.headers["location"] == (
        "/login?intent=safe_task_detail&task_id=task-1"
    )
    assert admin.status_code == 302
    assert admin.headers["location"] == "/login?intent=admin_center"
    assert response.status_code == 200
    assert "login-form" in response.text
    assert 'id="username"' in response.text
    assert "把复杂运维，收进一条可信工作流" in response.text
    assert "/app/api/tasks" not in response.text


async def test_oauth_flow_sets_host_only_secure_cookies_and_protected_shell(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        start = await client.get("/oauth/feishu/start")
        assert start.status_code == 302
        assert start.headers["location"].startswith("https://feishu.example.test/")
        state_headers = start.headers.get_list("set-cookie")
        assert len(state_headers) == 1
        assert state_headers[0].startswith(f"{_OAUTH_STATE_COOKIE_NAME}=")
        assert "Secure" in state_headers[0]
        assert "HttpOnly" in state_headers[0]
        assert "SameSite=lax" in state_headers[0]
        assert "Path=/" in state_headers[0]
        assert "Domain=" not in state_headers[0]

        callback = await client.get(
            "/oauth/feishu/callback",
            params={"code": "valid-code", "state": oauth.states[-1]},
        )
        assert callback.status_code == 302
        assert callback.headers["location"] == "/app"
        session_headers = callback.headers.get_list("set-cookie")
        session_header = next(
            header
            for header in session_headers
            if header.startswith(f"{_SESSION_COOKIE_NAME}=")
        )
        assert "Secure" in session_header
        assert "HttpOnly" in session_header
        assert "SameSite=lax" in session_header
        assert "Path=/" in session_header
        assert "Domain=" not in session_header
        assert any(
            header.startswith(f"{_OAUTH_STATE_COOKIE_NAME}=") and "Max-Age=0" in header
            for header in session_headers
        )

        shell = await client.get("/app")
        assert shell.status_code == 200
        assert shell.headers["content-type"].startswith("text/html")
        assert "小维 · 运维任务工作台" in shell.text
        assert '<script type="module" src="/app/static/app.js"></script>' in shell.text
        assert "<script>" not in shell.text

        me = await client.get("/app/api/me")
        assert me.status_code == 200
        assert me.json()["actor"] == "alice"
        assert me.json()["environment_id"] == "dev"
        assert me.json()["permissions"] == [
            "submit_readonly_task",
            "view_safe_task",
        ]
        assert len(me.json()["csrf_token"]) == 64


async def test_unknown_identity_callback_returns_activation_pending_without_session(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state, subject_ref="unknown-subject")
    async with _client(app) as client:
        start = await client.get("/oauth/feishu/start")
        assert start.status_code == 302
        callback = await client.get(
            "/oauth/feishu/callback",
            params={"code": "valid-code", "state": oauth.states[-1]},
        )

        assert callback.status_code == 302
        assert callback.headers["location"].startswith(
            "/login?intent=activation_status&request_id="
        )
        assert callback.headers["location"].endswith("&notice=pending")
        cookies = callback.headers.get_list("set-cookie")
        assert any(
            header.startswith(f"{_OAUTH_STATE_COOKIE_NAME}=") and "Max-Age=0" in header
            for header in cookies
        )
        assert not any(
            header.startswith(f"{_SESSION_COOKIE_NAME}=") for header in cookies
        )
        assert len(memory_state.activation_requests) == 1
        assert memory_state.web_sessions == {}


async def test_oauth_start_rejects_open_redirect_and_ambiguous_intents(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        for params in (
            [("next", "https://evil.example.test")],
            [("intent", "workbench"), ("intent", "admin_center")],
            [("intent", "safe_task_detail")],
            [("intent", "workbench"), ("task_id", "task-1")],
        ):
            response = await client.get("/oauth/feishu/start", params=params)
            assert response.status_code == 400
            assert response.json() == {"error": {"code": "invalid_request"}}
    assert oauth.states == []


async def test_operator_cannot_receive_an_admin_session(clock, memory_state) -> None:
    app, oauth = _web_app(clock, memory_state, role=ProductRole.OPERATOR)
    async with _client(app) as client:
        callback = await _login_for(client, oauth, intent="admin_center")
        assert callback.status_code == 302
        assert callback.headers["location"] == (
            "/login?intent=admin_center&notice=destination_not_available"
        )
        assert not any(
            header.startswith(f"{_SESSION_COOKIE_NAME}=")
            for header in callback.headers.get_list("set-cookie")
        )
        denied = await client.get(callback.headers["location"])
        assert denied.status_code == 403
        assert not any(
            header.startswith(f"{_SESSION_COOKIE_NAME}=")
            for header in denied.headers.get_list("set-cookie")
        )
        assert (await client.get("/admin")).status_code == 302


async def test_authenticated_operator_cannot_access_admin_routes(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state, role=ProductRole.OPERATOR)
    async with _client(app) as client:
        callback = await _login_for(client, oauth, intent="workbench")
        assert callback.status_code == 302
        assert callback.headers["location"] == "/app"

        for path in ("/admin", "/admin/api/integration-status"):
            response = await client.get(path)
            assert response.status_code == 403, path
            assert response.json() == {"error": {"code": "forbidden"}}, path


async def test_operator_cannot_read_raw_admin_configuration(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state, role=ProductRole.OPERATOR)
    async with _client(app) as client:
        assert (await _login_for(client, oauth, intent="workbench")).status_code == 302
        response = await client.get("/admin/api/config")

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "forbidden"}}


async def test_user_can_only_receive_a_safe_task_detail_session(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state, role=ProductRole.USER)
    async with _client(app) as client:
        denied = await _login_for(client, oauth, intent="workbench")
        assert denied.status_code == 302
        assert "destination_not_available" in denied.headers["location"]
        allowed = await _login_for(
            client,
            oauth,
            intent="safe_task_detail",
            task_id="task-1",
        )
        assert allowed.status_code == 302
        assert allowed.headers["location"] == "/app/tasks/task-1"
        assert (await client.get("/app/tasks/task-1")).status_code == 200
        assert (await client.get("/app/api/tasks")).status_code == 403
        assert (await client.post("/app/api/tasks", json={})).status_code == 403
        raw_config = await client.get("/admin/api/config")
        assert raw_config.status_code == 403
        assert raw_config.json() == {"error": {"code": "forbidden"}}


async def test_feishu_admin_reads_only_the_redacted_integration_projection(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state, role=ProductRole.ADMIN)
    async with _client(app) as client:
        callback = await _login_for(client, oauth, intent="admin_center")
        assert callback.status_code == 302
        assert callback.headers["location"] == "/admin"
        shell = await client.get("/admin")
        assert shell.status_code == 200
        assert "/app/static/admin.js" in shell.text

        status = await client.get("/admin/api/integration-status")
        assert status.status_code == 200
        payload = status.json()
        assert set(payload) == {"domains"}
        assert [item["domain"] for item in payload["domains"]] == [
            "ai",
            "feishu",
            "resources",
        ]
        assert all(
            set(item)
            == {
                "domain",
                "configured",
                "restart_required",
                "load_status",
                "last_test_status",
                "last_tested_at",
            }
            for item in payload["domains"]
        )
        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "generation",
            "app_id",
            "secret",
            "endpoint",
            "error_code",
        ):
            assert forbidden not in serialized.lower()
        assert (await client.get("/admin/api/config")).status_code == 403


async def test_duplicate_callback_parameters_are_rejected_before_exchange(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        start = await client.get("/oauth/feishu/start")
        assert start.status_code == 302
        callback = await client.get(
            "/oauth/feishu/callback",
            params=[
                ("code", "valid-code"),
                ("code", "valid-code"),
                ("state", oauth.states[-1]),
            ],
        )

        assert callback.status_code == 400
        assert callback.json() == {"error": {"code": "invalid_request"}}
        assert oauth.exchange_calls == 0
        assert not any(
            header.startswith(f"{_SESSION_COOKIE_NAME}=")
            for header in callback.headers.get_list("set-cookie")
        )


async def test_duplicate_auth_cookies_are_rejected_without_choosing_a_value(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        assert (await client.get("/oauth/feishu/start")).status_code == 302
        state = oauth.states[-1]
        client.cookies.clear()
        callback = await client.get(
            "/oauth/feishu/callback",
            params={"code": "valid-code", "state": state},
            headers={
                "cookie": (
                    f"{_OAUTH_STATE_COOKIE_NAME}={state}; "
                    f"{_OAUTH_STATE_COOKIE_NAME}={state}"
                )
            },
        )
        assert callback.status_code == 400
        assert callback.json() == {"error": {"code": "invalid_request"}}
        assert oauth.exchange_calls == 0

    async with _client(app) as client:
        assert (await _login(client, oauth)).status_code == 302
        session = client.cookies.get(_SESSION_COOKIE_NAME)
        assert session is not None
        client.cookies.clear()
        response = await client.get(
            "/app/api/me",
            headers={
                "cookie": (
                    f"{_SESSION_COOKIE_NAME}={session}; "
                    f"{_SESSION_COOKIE_NAME}={session}"
                )
            },
        )
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "unauthorized"}}


async def test_logout_requires_json_origin_csrf_and_revokes_session(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        assert (await _login(client, oauth)).status_code == 302
        me = await client.get("/app/api/me")
        csrf = me.json()["csrf_token"]

        cases = (
            ({"content": b"{}"}, 415, "unsupported_media_type"),
            (
                {
                    "json": {},
                    "headers": {
                        "origin": "https://evil.example.test",
                        "x-csrf-token": csrf,
                    },
                },
                403,
                "forbidden",
            ),
            (
                {"json": {}, "headers": {"origin": "https://ops.example.test"}},
                403,
                "forbidden",
            ),
            (
                {
                    "content": b"not-json",
                    "headers": {
                        "content-type": "application/json",
                        "origin": "https://ops.example.test",
                        "x-csrf-token": csrf,
                    },
                },
                400,
                "invalid_request",
            ),
        )
        for kwargs, status, code in cases:
            response = await client.post("/app/api/logout", **kwargs)
            assert response.status_code == status
            assert response.json() == {"error": {"code": code}}

        for duplicate_headers in (
            [
                ("content-type", "application/json"),
                ("origin", "https://ops.example.test"),
                ("origin", "https://ops.example.test"),
                ("x-csrf-token", csrf),
            ],
            [
                ("content-type", "application/json"),
                ("origin", "https://ops.example.test"),
                ("x-csrf-token", csrf),
                ("x-csrf-token", csrf),
            ],
        ):
            response = await client.post(
                "/app/api/logout",
                content=b"{}",
                headers=duplicate_headers,
            )
            assert response.status_code == 403
            assert response.json() == {"error": {"code": "forbidden"}}

        logged_out = await client.post(
            "/app/api/logout",
            json={},
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": csrf,
            },
        )
        assert logged_out.status_code == 204
        assert any(
            header.startswith(f"{_SESSION_COOKIE_NAME}=") and "Max-Age=0" in header
            for header in logged_out.headers.get_list("set-cookie")
        )
        assert (await client.get("/app/api/me")).status_code == 401


async def test_web_routes_and_internal_routes_are_mutually_closed(
    clock, memory_state
) -> None:
    web, _ = _web_app(clock, memory_state)
    assert {
        (method, route.path)
        for route in web.routes
        for method in getattr(route, "methods", set())
    } == {
        ("GET", "/oauth/feishu/start"),
        ("GET", "/oauth/feishu/callback"),
        ("GET", "/login"),
        ("GET", "/app"),
        ("GET", "/admin"),
        ("GET", "/app/tasks/{task_id}"),
        ("GET", "/app/api/me"),
        ("GET", "/app/api/tasks"),
        ("POST", "/app/api/tasks"),
        ("GET", "/app/api/tasks/{task_id}"),
        ("POST", "/app/api/logout"),
        ("POST", "/login/api/login"),
        ("POST", "/login/api/change-password"),
        ("GET", "/admin/api/integration-status"),
        ("GET", "/admin/api/config"),
        ("PUT", "/admin/api/config"),
        ("POST", "/admin/api/config/clear"),
        # 字面量在前、路径参数在后：Starlette 按注册顺序匹配，反过来会让
        # feishu_oauth 落进凭据探针那条分支。
        ("POST", "/admin/api/config/test/feishu_oauth"),
        ("POST", "/admin/api/config/test/{check_name}"),
        ("GET", "/admin/api/users"),
        ("POST", "/admin/api/users/status"),
        ("POST", "/admin/api/users/role"),
        ("GET", "/admin/api/activations"),
        ("POST", "/admin/api/activations/approve"),
        ("POST", "/admin/api/activations/reject"),
        ("GET", "/admin/api/audit"),
        ("GET", "/app/static/app.css"),
        ("GET", "/app/static/app.js"),
        ("GET", "/app/static/admin.js"),
        ("GET", "/app/static/detail.js"),
        ("GET", "/app/static/login.js"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
    }

    class Runtime:
        async def submit_task(self, *, submission: object) -> object:
            raise AssertionError(submission)

        async def query_task(self, *, lookup: object) -> object:
            raise AssertionError(lookup)

    internal = create_internal_app(
        runtime=Runtime(),
        settings=Settings(environment_id="dev"),
        readiness=_Probe(),
        clock=clock,
        policy_revision="policy-1",
    )
    async with _client(web) as client:
        for method, path in (
            ("GET", "/v1/tasks"),
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tasks/task-1"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 404
            assert response.json() == {"error": {"code": "not_found"}}
    async with _client(internal) as client:
        for method, path in (
            ("GET", "/app"),
            ("GET", "/app/api/me"),
            ("POST", "/app/api/logout"),
            ("GET", "/oauth/feishu/start"),
            ("GET", "/oauth/feishu/callback?code=x&state=y"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 404
            assert response.json() == {"error": {"code": "not_found"}}


async def test_every_response_has_browser_security_headers_and_no_cache(
    clock, memory_state
) -> None:
    app, _ = _web_app(clock, memory_state)
    async with _client(app) as client:
        for path in ("/missing", "/app", "/healthz", "/readyz"):
            response = await client.get(path)
            csp = response.headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "script-src 'self'" in csp
            assert "object-src 'none'" in csp
            assert "unsafe-inline" not in csp
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["strict-transport-security"] == (
                "max-age=31536000"
            )


async def test_static_assets_are_served_from_exact_routes_with_safe_media_types(
    clock, memory_state
) -> None:
    app, _ = _web_app(clock, memory_state)
    expected = {
        "/app/static/app.css": ("text/css", ".workbench-main"),
        "/app/static/app.js": ("text/javascript", "function renderTaskList"),
        "/app/static/admin.js": ("text/javascript", "function renderConfig"),
        "/app/static/detail.js": ("text/javascript", "function clearTaskDetail"),
        "/app/static/login.js": ("text/javascript", 'meta[name="csrf-token"]'),
    }

    async with _client(app) as client:
        for path, (media_type, marker) in expected.items():
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(media_type)
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert marker in response.text
        assert (await client.get("/app/static/missing.js")).status_code == 404


async def test_health_readiness_and_framework_errors_use_closed_bodies(
    clock, memory_state
) -> None:
    app, _ = _web_app(clock, memory_state)
    async with _client(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/missing")).json() == {
            "error": {"code": "not_found"}
        }
        method = await client.delete("/app")
        assert method.status_code == 405
        assert method.json() == {"error": {"code": "method_not_allowed"}}
        for path in ("/docs", "/redoc", "/openapi.json", "/app/"):
            assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", b"127.0.0.1:8080")],
        [(b"host", b"evil.example.test")],
        [(b"host", b"ops.example.test:8443")],
        [(b"host", b"user@ops.example.test")],
        [(b"host", b"ops.example.test:")],
        [(b"host", b"ops.example.test:0443")],
        [(b"host", b"ops.example.test:0")],
        [(b"host", b"ops.example.test?")],
        [(b"host", b"ops.example.test#")],
        [(b"host", b"127.1")],
        [(b"host", b"127.000.000.001")],
        [(b"host", b"2130706433")],
        [(b"host", b"0x7f000001")],
        [(b"host", b"0177.0.0.1")],
        [(b"host", b"0x7f.1")],
        [
            (b"host", b"ops.example.test"),
            (b"host", b"ops.example.test"),
        ],
        [],
    ],
    ids=[
        "direct-ip",
        "wrong-domain",
        "wrong-port",
        "userinfo",
        "empty-port",
        "noncanonical-port",
        "zero-port",
        "empty-query",
        "empty-fragment",
        "short-ipv4",
        "zero-padded-ipv4",
        "decimal-ipv4",
        "hex-ipv4",
        "octal-component-ipv4",
        "hex-component-ipv4",
        "duplicate",
        "missing",
    ],
)
async def test_non_health_routes_reject_untrusted_direct_authority_before_oauth(
    clock, memory_state, headers: list[tuple[bytes, bytes]]
) -> None:
    app, oauth = _web_app(clock, memory_state)

    status, body, response_headers = await _raw_get(
        app, path="/oauth/feishu/start", headers=headers
    )

    assert status == 403
    assert body == {"error": {"code": "forbidden"}}
    assert oauth.states == []
    assert (b"cache-control", b"no-store") in response_headers


async def test_direct_host_is_authoritative_and_forwarded_headers_are_ignored(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        accepted = await client.get(
            "/oauth/feishu/start",
            headers={
                "host": "OPS.EXAMPLE.TEST:443",
                "forwarded": "host=evil.example.test;proto=http",
                "x-forwarded-host": "evil.example.test",
                "x-forwarded-proto": "http",
            },
        )
        rejected = await client.get(
            "/oauth/feishu/start",
            headers={
                "host": "evil.example.test",
                "forwarded": "host=ops.example.test;proto=https",
                "x-forwarded-host": "ops.example.test",
                "x-forwarded-proto": "https",
            },
        )

    assert accepted.status_code == 302
    assert rejected.status_code == 403
    assert rejected.json() == {"error": {"code": "forbidden"}}
    assert len(oauth.states) == 1


async def test_configured_custom_https_authority_requires_its_exact_port(
    clock, memory_state
) -> None:
    public_origin = "https://ops.example.test:8443"
    app, oauth = _web_app(
        clock,
        memory_state,
        public_origin=public_origin,
    )
    async with _client(app, base_url=public_origin) as client:
        accepted = await client.get("/oauth/feishu/start")
        missing_port = await client.get(
            "/oauth/feishu/start", headers={"host": "ops.example.test"}
        )
        default_port = await client.get(
            "/oauth/feishu/start", headers={"host": "ops.example.test:443"}
        )

    assert accepted.status_code == 302
    assert missing_port.status_code == 403
    assert default_port.status_code == 403
    assert len(oauth.states) == 1


async def test_non_ascii_raw_host_cannot_alias_a_canonical_idna_authority(
    clock, memory_state
) -> None:
    app, oauth = _web_app(
        clock,
        memory_state,
        public_origin="https://xn--wda.example.test",
    )

    status, body, _ = await _raw_get(
        app,
        path="/oauth/feishu/start",
        headers=[(b"host", b"\xff.example.test")],
    )

    assert status == 403
    assert body == {"error": {"code": "forbidden"}}
    assert oauth.states == []


async def test_loopback_http_scheme_is_allowed_only_with_the_sso_host(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    ) as client:
        proxied = await client.get(
            "/oauth/feishu/start", headers={"host": "ops.example.test"}
        )
        direct = await client.get(
            "/oauth/feishu/start", headers={"host": "127.0.0.1:8080"}
        )

    assert proxied.status_code == 302
    assert direct.status_code == 403
    assert len(oauth.states) == 1


async def test_non_health_get_origin_is_optional_but_strict_when_present(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        rejected = await client.get(
            "/oauth/feishu/start",
            headers={"origin": "https://evil.example.test"},
        )
        duplicate = await client.get(
            "/oauth/feishu/start",
            headers=[
                ("origin", "https://ops.example.test"),
                ("origin", "https://ops.example.test"),
            ],
        )
        accepted = await client.get(
            "/oauth/feishu/start",
            headers={"origin": "https://ops.example.test"},
        )

    assert rejected.status_code == 403
    assert duplicate.status_code == 403
    assert accepted.status_code == 302
    assert len(oauth.states) == 1


async def test_health_routes_are_the_only_direct_ip_host_exceptions(
    clock, memory_state
) -> None:
    app, oauth = _web_app(clock, memory_state)
    async with _client(app) as client:
        health = await client.get("/healthz", headers={"host": "127.0.0.1:8080"})
        ready = await client.get("/readyz", headers={"host": "127.0.0.1:8080"})
        protected = await client.get("/app", headers={"host": "127.0.0.1:8080"})

    assert health.status_code == 200
    assert ready.status_code == 200
    assert protected.status_code == 403
    assert oauth.states == []


async def test_oauth_request_trace_is_server_generated_bound_and_logged_once(
    clock, memory_state, caplog: pytest.LogCaptureFixture
) -> None:
    app, oauth = _web_app(clock, memory_state)
    incoming = "f" * 32
    with caplog.at_level(logging.INFO, logger="xiaowei_agent.interfaces.web_app"):
        async with _client(app) as client:
            started = await client.get(
                "/oauth/feishu/start", headers={"x-trace-id": incoming}
            )
            callback = await client.get(
                "/oauth/feishu/callback",
                params={"code": "valid-code", "state": oauth.states[-1]},
                headers={"x-trace-id": incoming},
            )

    assert started.status_code == 302
    assert callback.status_code == 302
    assert len(oauth.authorization_trace_ids) == 1
    start_trace_id = oauth.authorization_trace_ids[0]
    assert len(oauth.exchange_trace_ids) == 1
    callback_trace_id = oauth.exchange_trace_ids[0]
    assert oauth.identity_trace_ids == [callback_trace_id]
    assert start_trace_id is not None
    assert callback_trace_id is not None
    assert start_trace_id != callback_trace_id
    assert incoming not in {start_trace_id, callback_trace_id}
    records = [
        record
        for record in caplog.records
        if record.name == "xiaowei_agent.interfaces.web_app"
    ]
    assert [record.route_class for record in records] == [
        "oauth_start",
        "oauth_callback",
    ]
    assert [record.outcome for record in records] == ["ok", "ok"]
    assert [record.trace_id for record in records] == [
        start_trace_id,
        callback_trace_id,
    ]


_FAKE_APP_SECRET = "unit-test-" + "app-secret"

async def test_serve_web_assembles_real_ports_with_fixed_oauth_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: dict[str, object] = {}

    class OAuth:
        def __init__(self, **kwargs: object) -> None:
            events["oauth"] = kwargs
            events["oauth_instance"] = self

    class Membership:
        def __init__(self, **kwargs: object) -> None:
            events["membership"] = kwargs
            events["membership_instance"] = self

    class Stack:
        auth = object()
        local_admin_auth = NoLocalAdmin()
        oauth_available = True
        provider_state = EmptyProviderState()
        readiness = _Probe()
        task_access_service = object()
        submission_service = object()
        clock = object()
        policy_revision = "policy-1"
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    stack = Stack()

    async def build(**kwargs: object) -> Stack:
        events["build"] = kwargs
        return stack

    def app_factory(**kwargs: object) -> object:
        events["app"] = kwargs
        return SimpleNamespace()

    class Server:
        def __init__(self, config: uvicorn.Config) -> None:
            events["server_config"] = config

        async def serve(self) -> None:
            events["served"] = True

    # RI5：凭据来自 `integrations.json`，composition root 先读一次再构造 adapter。
    monkeypatch.setattr(
        provider_consumption_module,
        "load_provider_credentials",
        lambda **_: (
            ProviderCredentials(
                feishu_app_id="cli_test_app", feishu_app_secret=_FAKE_APP_SECRET
            ),
            {},
        ),
    )
    monkeypatch.setattr(feishu_oauth_module, "FeishuOAuthAdapter", OAuth)
    monkeypatch.setattr(feishu_sdk_module, "FeishuSdkMembershipAdapter", Membership)
    monkeypatch.setattr(local_stack_module, "build_postgres_web_stack", build)
    monkeypatch.setattr(web_app_module, "create_app", app_factory)
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)
    monkeypatch.setattr(web_app_module.uvicorn, "Server", Server)
    settings = Settings(
        **(
            _settings().model_dump()
            | {"feishu_api_timeout_seconds": 10.0}
        )
    )

    result = await web_app_module.serve_web(settings)

    assert result == 0
    assert events["oauth"] == {
        "app_id": "cli_test_app",
        "app_secret": _FAKE_APP_SECRET,
        "timeout_seconds": web_auth_module.FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS,
    }
    assert events["membership"] == {
        "tenant_id": "dev-local",
        "app_id": "cli_test_app",
        "app_secret": _FAKE_APP_SECRET,
    }
    assert events["build"] == {
        "settings": settings,
        "oauth": events["oauth_instance"],
        "membership": events["membership_instance"],
    }
    config = events["server_config"]
    assert isinstance(config, uvicorn.Config)
    assert config.host == "127.0.0.1"
    assert config.port == 8080
    assert config.proxy_headers is False
    assert config.access_log is False
    assert config.log_config is None
    assert events["served"] is True
    assert stack.close_calls == 1


def test_real_module_entry_rejects_half_enabled_web_before_secret_access() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env.update(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_FEISHU_IDENTITY_FILE": "/missing/identity-reference",
            "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://ops.example.test",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "xiaowei_agent.interfaces.web_app"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "xiaowei-web: configuration_error\n"


def test_main_maps_identity_configuration_to_one_fixed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class OAuth:
        def __init__(self, **_: object) -> None:
            return None

    class Membership:
        def __init__(self, **_: object) -> None:
            return None

    async def fail_identity(**_: object) -> object:
        raise local_stack_module.WebStackConfigurationError(
            "identity-sensitive-detail"
        )

    monkeypatch.setattr(web_app_module, "load_settings", lambda: _settings())
    # RI5：凭据来自 `integrations.json`，composition root 先读一次再构造 adapter。
    monkeypatch.setattr(
        provider_consumption_module,
        "load_provider_credentials",
        lambda **_: (
            ProviderCredentials(
                feishu_app_id="cli_test_app", feishu_app_secret=_FAKE_APP_SECRET
            ),
            {},
        ),
    )
    monkeypatch.setattr(feishu_oauth_module, "FeishuOAuthAdapter", OAuth)
    monkeypatch.setattr(feishu_sdk_module, "FeishuSdkMembershipAdapter", Membership)
    monkeypatch.setattr(local_stack_module, "build_postgres_web_stack", fail_identity)
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)

    result = web_app_module.main()

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "xiaowei-web: configuration_error\n"
    assert "sensitive" not in captured.err


def test_main_maps_database_credential_configuration_to_one_fixed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class OAuth:
        def __init__(self, **_: object) -> None:
            return None

    class Membership:
        def __init__(self, **_: object) -> None:
            return None

    async def fail_database(**_: object) -> object:
        raise local_stack_module.WebStackConfigurationError(
            "database-sensitive-reference"
        )

    monkeypatch.setattr(web_app_module, "load_settings", lambda: _settings())
    # RI5：凭据来自 `integrations.json`，composition root 先读一次再构造 adapter。
    monkeypatch.setattr(
        provider_consumption_module,
        "load_provider_credentials",
        lambda **_: (
            ProviderCredentials(
                feishu_app_id="cli_test_app", feishu_app_secret=_FAKE_APP_SECRET
            ),
            {},
        ),
    )
    monkeypatch.setattr(feishu_oauth_module, "FeishuOAuthAdapter", OAuth)
    monkeypatch.setattr(feishu_sdk_module, "FeishuSdkMembershipAdapter", Membership)
    monkeypatch.setattr(local_stack_module, "build_postgres_web_stack", fail_database)
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)

    result = web_app_module.main()

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "xiaowei-web: configuration_error\n"
    assert "sensitive" not in captured.err


def test_main_serves_the_web_without_feishu_oauth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()`` 就是 Compose 的 web-app 进程入口。

    真实部署走的是 ``python -m xiaowei_agent.interfaces.web_app``，因此"飞书可选"
    这件事必须在 ``main()`` 成立。只在 ``serve_web()`` 里解耦等于没有解耦：进程
    在更外面一层就退出了，本地管理员永远没有机会登录。
    """
    web_only = Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=False,
        web_mode=WebMode.LAN_HTTP,
        web_public_origin="http://127.0.0.1:8080",
    )
    served: list[Settings] = []

    async def serve(value: Settings) -> int:
        served.append(value)
        return 0

    monkeypatch.setattr(web_app_module, "load_settings", lambda: web_only)
    monkeypatch.setattr(web_app_module, "serve_web", serve, raising=False)

    result = web_app_module.main()

    captured = capsys.readouterr()
    assert result == 0
    assert served == [web_only]
    assert captured.err == ""


def test_main_without_the_web_app_returns_two_without_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """反例：默认关闭仍然是唯一的"不启动"判据，而且它不是配置错误。"""

    async def must_not_serve(_: Settings) -> int:
        raise AssertionError("a disabled Web app must not assemble anything")

    monkeypatch.setattr(
        web_app_module,
        "load_settings",
        lambda: Settings(environment_id="dev", web_app_enabled=False),
    )
    monkeypatch.setattr(web_app_module, "serve_web", must_not_serve, raising=False)

    result = web_app_module.main()

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == ""


def test_main_maps_nonconfiguration_failure_to_one_fixed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail_runtime(_: Settings) -> int:
        raise RuntimeError("provider-sensitive-detail")

    monkeypatch.setattr(web_app_module, "load_settings", lambda: _settings())
    monkeypatch.setattr(web_app_module, "serve_web", fail_runtime, raising=False)

    result = web_app_module.main()

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "xiaowei-web: startup_failed\n"
    assert "sensitive" not in captured.err


def test_main_maps_uvicorn_startup_exit_to_one_fixed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail_startup(_: Settings) -> int:
        raise SystemExit("provider-sensitive-detail")

    monkeypatch.setattr(web_app_module, "load_settings", lambda: _settings())
    monkeypatch.setattr(web_app_module, "serve_web", fail_startup, raising=False)

    result = web_app_module.main()

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "xiaowei-web: startup_failed\n"
    assert "sensitive" not in captured.err


def test_real_uvicorn_bind_failure_emits_only_the_fixed_main_error(
    tmp_path: Path,
) -> None:
    postgres_secret = tmp_path / "postgres-credential"
    postgres_secret.write_text("fixture-" + "password", encoding="utf-8")
    feishu_secret = tmp_path / "feishu-credential"
    feishu_secret.write_text("fixture-" + "secret", encoding="utf-8")
    identities = tmp_path / "identities.json"
    identities.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    handler_sentinel = tmp_path / "uvicorn-error-handler-called"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env.update(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_POSTGRES_PASSWORD_FILE": str(postgres_secret),
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
            "XIAOWEI_FEISHU_IDENTITY_FILE": str(identities),
            "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://ops.example.test",
            "XIAOWEI_WEB_BIND_HOST": "192.0.2.1",
            "XIAOWEI_WEB_BIND_PORT": "49152",
            "TEST_UVICORN_HANDLER_SENTINEL": str(handler_sentinel),
        }
    )
    script = """
import logging
import os
import sys
from pathlib import Path

from xiaowei_agent.interfaces import provider_consumption
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials
from xiaowei_agent.interfaces.web_app import main

# RI5：凭据真源是 `integrations.json`。本用例要验的是**真实 uvicorn 绑定失败**那一段，
# 因此在子进程里把读取替换成固定返回，避免依赖容器里的挂载路径。
provider_consumption.load_provider_credentials = lambda **_: (
    ProviderCredentials(
        feishu_app_id="cli_test_app", feishu_app_secret="fixture-" + "secret"
    ),
    {},
)


class FailingErrorHandler(logging.Handler):
    def emit(self, record):
        Path(os.environ["TEST_UVICORN_HANDLER_SENTINEL"]).write_text(
            "called", encoding="utf-8"
        )
        raise RuntimeError("unsafe logging handler failed")

logger = logging.getLogger("uvicorn.error")
logger.handlers.clear()
logger.propagate = False
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stderr))
failing = FailingErrorHandler()
failing.setLevel(logging.ERROR)
logger.addHandler(failing)
raise SystemExit(main())
"""

    completed = subprocess.run(  # noqa: S603 - 固定解释器与本地常量脚本。
        [sys.executable, "-c", script],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "xiaowei-web: startup_failed\n"
    assert not handler_sentinel.exists()


@pytest.mark.parametrize("failure_point", ["app", "config", "server", "serve"])
async def test_serve_web_closes_stack_at_every_post_assembly_failure(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    class OAuth:
        def __init__(self, **_: object) -> None:
            return None

    class Membership:
        def __init__(self, **_: object) -> None:
            return None

    class Stack:
        auth = object()
        local_admin_auth = NoLocalAdmin()
        oauth_available = True
        provider_state = EmptyProviderState()
        readiness = _Probe()
        task_access_service = object()
        submission_service = object()
        clock = object()
        policy_revision = "policy-1"
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    stack = Stack()

    async def build(**_: object) -> Stack:
        return stack

    def app_factory(**_: object) -> object:
        if failure_point == "app":
            raise RuntimeError("app failed")
        return SimpleNamespace()

    real_config = uvicorn.Config

    def config_factory(*args: object, **kwargs: object) -> uvicorn.Config:
        if failure_point == "config":
            raise RuntimeError("config failed")
        return real_config(*args, **kwargs)

    class Server:
        def __init__(self, _: uvicorn.Config) -> None:
            if failure_point == "server":
                raise RuntimeError("server failed")

        async def serve(self) -> None:
            if failure_point == "serve":
                raise RuntimeError("serve failed")

    # RI5：凭据来自 `integrations.json`，composition root 先读一次再构造 adapter。
    monkeypatch.setattr(
        provider_consumption_module,
        "load_provider_credentials",
        lambda **_: (
            ProviderCredentials(
                feishu_app_id="cli_test_app", feishu_app_secret=_FAKE_APP_SECRET
            ),
            {},
        ),
    )
    monkeypatch.setattr(feishu_oauth_module, "FeishuOAuthAdapter", OAuth)
    monkeypatch.setattr(feishu_sdk_module, "FeishuSdkMembershipAdapter", Membership)
    monkeypatch.setattr(local_stack_module, "build_postgres_web_stack", build)
    monkeypatch.setattr(web_app_module, "create_app", app_factory)
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)
    monkeypatch.setattr(web_app_module.uvicorn, "Config", config_factory)
    monkeypatch.setattr(web_app_module.uvicorn, "Server", Server)

    with pytest.raises(RuntimeError, match="failed"):
        await web_app_module.serve_web(_settings())

    assert stack.close_calls == 1


def test_real_module_entry_is_silent_and_closed_with_default_profile() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env["XIAOWEI_ENVIRONMENT_ID"] = "dev"

    completed = subprocess.run(
        [sys.executable, "-m", "xiaowei_agent.interfaces.web_app"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_enabled_module_entry_maps_bad_secret_reference_to_configuration_error() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env.update(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
            "XIAOWEI_FEISHU_IDENTITY_FILE": "/missing/identity-reference",
            "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://ops.example.test",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "xiaowei_agent.interfaces.web_app"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "xiaowei-web: configuration_error\n"

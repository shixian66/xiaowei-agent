"""隔离 Web app 的 OAuth、Cookie、安全头与路由闭集。"""

import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import httpx

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    ReadinessReport,
)
from xiaowei_agent.interfaces.api import create_app as create_internal_app
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.web_app import (
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_app,
)
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity, WebAuthService
from xiaowei_agent.persistence.fake import InMemoryWebSessionStore


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
    def __init__(self, subject_ref: str = "subject-alice") -> None:
        self.subject_ref = subject_ref
        self.states: list[str] = []
        self.exchange_calls = 0

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        self.states.append(state)
        assert redirect_uri == "https://ops.example.test/oauth/feishu/callback"
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        assert code == "valid-code"
        assert redirect_uri == "https://ops.example.test/oauth/feishu/callback"
        self.exchange_calls += 1
        return FeishuOAuthIdentity(subject_ref=self.subject_ref)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="subject-alice",
        permissions=frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    )


def _settings() -> Settings:
    return Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )


def _tokens() -> Iterator[str]:
    yield from (
        "state_value_first_1234567890",
        "session_value_first_1234567890",
        "state_value_second_1234567890",
        "session_value_second_1234567890",
    )


def _web_app(clock, memory_state, *, subject_ref: str = "subject-alice") -> Any:
    oauth = _OAuth(subject_ref=subject_ref)
    token_values = _tokens()
    principal = _principal()
    auth = WebAuthService(
        sessions=InMemoryWebSessionStore(clock=clock, state=memory_state),
        identities=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        oauth=oauth,
        public_origin="https://ops.example.test",
        oauth_state_ttl_seconds=300,
        session_ttl_seconds=3600,
        token_factory=lambda: next(token_values),
    )
    return create_app(auth=auth, settings=_settings(), readiness=_Probe()), oauth


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient, oauth: _OAuth) -> httpx.Response:
    start = await client.get("/oauth/feishu/start")
    assert start.status_code == 302
    return await client.get(
        "/oauth/feishu/callback",
        params={"code": "valid-code", "state": oauth.states[-1]},
    )


async def test_real_protected_routes_return_401_unauthorized(clock, memory_state) -> None:
    app, _ = _web_app(clock, memory_state)
    async with _client(app) as client:
        for path in ("/app", "/app/api/me"):
            response = await client.get(path)
            assert response.status_code == 401
            assert response.json() == {"error": {"code": "unauthorized"}}


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
        assert state_headers[0].startswith(f"{OAUTH_STATE_COOKIE_NAME}=")
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
            if header.startswith(f"{SESSION_COOKIE_NAME}=")
        )
        assert "Secure" in session_header
        assert "HttpOnly" in session_header
        assert "SameSite=lax" in session_header
        assert "Path=/" in session_header
        assert "Domain=" not in session_header
        assert any(
            header.startswith(f"{OAUTH_STATE_COOKIE_NAME}=") and "Max-Age=0" in header
            for header in session_headers
        )

        shell = await client.get("/app")
        assert shell.status_code == 200
        assert shell.headers["content-type"].startswith("text/html")
        assert "小维 · 运维任务工作台" in shell.text
        assert "<script" not in shell.text

        me = await client.get("/app/api/me")
        assert me.status_code == 200
        assert me.json()["actor"] == "alice"
        assert me.json()["environment_id"] == "dev"
        assert me.json()["permissions"] == [
            "submit_readonly_task",
            "view_safe_task",
        ]
        assert len(me.json()["csrf_token"]) == 64


async def test_callback_rejections_clear_state_and_never_set_a_session(
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

        assert callback.status_code == 401
        assert callback.json() == {"error": {"code": "unauthorized"}}
        cookies = callback.headers.get_list("set-cookie")
        assert any(
            header.startswith(f"{OAUTH_STATE_COOKIE_NAME}=") and "Max-Age=0" in header
            for header in cookies
        )
        assert not any(
            header.startswith(f"{SESSION_COOKIE_NAME}=") for header in cookies
        )


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
            header.startswith(f"{SESSION_COOKIE_NAME}=")
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
                    f"{OAUTH_STATE_COOKIE_NAME}={state}; "
                    f"{OAUTH_STATE_COOKIE_NAME}={state}"
                )
            },
        )
        assert callback.status_code == 400
        assert callback.json() == {"error": {"code": "invalid_request"}}
        assert oauth.exchange_calls == 0

    async with _client(app) as client:
        assert (await _login(client, oauth)).status_code == 302
        session = client.cookies.get(SESSION_COOKIE_NAME)
        assert session is not None
        client.cookies.clear()
        response = await client.get(
            "/app/api/me",
            headers={
                "cookie": (
                    f"{SESSION_COOKIE_NAME}={session}; "
                    f"{SESSION_COOKIE_NAME}={session}"
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
            header.startswith(f"{SESSION_COOKIE_NAME}=") and "Max-Age=0" in header
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
        ("GET", "/app"),
        ("GET", "/app/api/me"),
        ("POST", "/app/api/logout"),
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
            ("GET", "/app/api/tasks"),
            ("GET", "/app/tasks/task-1"),
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


async def test_every_response_has_a_strict_csp_and_no_cache(clock, memory_state) -> None:
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


def test_enabled_module_entry_does_not_activate_real_oauth_or_read_secrets() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env.update(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_FEISHU_APP_ID": "app",
            "XIAOWEI_FEISHU_APP_SECRET_FILE": "/missing/secret-reference",
            "XIAOWEI_FEISHU_IDENTITY_FILE": "/missing/identity-reference",
            "XIAOWEI_WEB_DETAIL_BASE_URL": "https://ops.example.test",
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
    assert completed.stderr == "xiaowei-web: oauth_adapter_not_activated\n"

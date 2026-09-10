"""Web OAuth 边界不泄漏外部错误，也不接受可漂移的 state/origin。"""

import asyncio
import logging
import traceback
from collections.abc import Iterator
from dataclasses import fields

import httpx
import pytest
from tests.security.test_task_view_runtime_authority import (
    _TASK_VIEW_PROCESS_ALLOWED_MODULES,
    _loaded_xiaowei_modules_after,
)

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    ReadinessReport,
)
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.local_stack import WebStack
from xiaowei_agent.interfaces.web_app import create_app
from xiaowei_agent.interfaces.web_auth import (
    FeishuOAuthIdentity,
    WebAuthenticationError,
    WebAuthService,
    WebOAuthCodeError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
)
from xiaowei_agent.persistence.fake import InMemoryWebSessionStore

pytestmark = pytest.mark.security


class _Probe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True,
            revision_matches_head=True,
            assembled=True,
        )


class _UnusedTaskAccess:
    async def list_tasks(self, *, query: object) -> object:
        raise AssertionError(query)

    async def get_task(self, *, query: object) -> object:
        raise AssertionError(query)


class _UnusedSubmissions:
    async def submit(self, *, command: object) -> object:
        raise AssertionError(command)


def _auth_app(*, service: WebAuthService, settings: Settings, clock) -> object:
    return create_app(
        auth=service,
        settings=settings,
        readiness=_Probe(),
        task_access=_UnusedTaskAccess(),
        submissions=_UnusedSubmissions(),
        clock=clock,
        policy_revision="policy-2026-09-01",
    )


class _OAuth:
    def __init__(
        self,
        *,
        subject_ref: str = "subject-alice",
        authorization_url: str | None = None,
        exchange_error: Exception | None = None,
    ) -> None:
        self.subject_ref = subject_ref
        self.authorization_url_value = authorization_url
        self.exchange_error = exchange_error
        self.states: list[str] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        del redirect_uri
        self.states.append(state)
        return self.authorization_url_value or (
            f"https://feishu.example.test/authorize?state={state}"
        )

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        del code, redirect_uri
        if self.exchange_error is not None:
            raise self.exchange_error
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


def _tokens() -> Iterator[str]:
    yield from (
        "state_value_first_1234567890",
        "session_value_first_1234567890",
        "state_value_second_1234567890",
        "session_value_second_1234567890",
    )


def _service(
    clock,
    memory_state,
    *,
    oauth: _OAuth,
    oauth_state_capacity: int = 1024,
) -> WebAuthService:
    principal = _principal()
    tokens = _tokens()
    return WebAuthService(
        sessions=InMemoryWebSessionStore(
            clock=clock,
            state=memory_state,
            oauth_state_capacity=oauth_state_capacity,
        ),
        identities=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        oauth=oauth,
        public_origin="https://ops.example.test",
        oauth_state_ttl_seconds=300,
        session_ttl_seconds=3600,
        token_factory=lambda: next(tokens),
    )


async def test_provider_exception_is_mapped_without_retaining_sensitive_context(
    clock, memory_state
) -> None:
    provider_detail = "token=" + "provider-sensitive-value"
    oauth = _OAuth(exchange_error=RuntimeError(provider_detail))
    service = _service(clock, memory_state, oauth=oauth)
    start = await service.start_login()

    try:
        await service.complete_login(
            code="provider-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )
    except WebOAuthUnavailableError as exc:
        rendered = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        assert provider_detail not in rendered
        assert provider_detail not in repr(exc.__dict__)
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("expected WebOAuthUnavailableError")


async def test_authorization_builder_exception_drops_sensitive_context(
    clock, memory_state
) -> None:
    provider_detail = "ticket=" + "provider-sensitive-value"

    class _FailingOAuth(_OAuth):
        def authorization_url(self, *, state: str, redirect_uri: str) -> str:
            del state, redirect_uri
            raise RuntimeError(provider_detail)

    service = _service(clock, memory_state, oauth=_FailingOAuth())

    try:
        await service.start_login()
    except WebOAuthUnavailableError as exc:
        rendered = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        assert provider_detail not in rendered
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("expected WebOAuthUnavailableError")


async def test_oauth_start_capacity_returns_503_without_cookie_body_or_log_leakage(
    clock, memory_state, caplog
) -> None:
    oauth = _OAuth()
    service = _service(
        clock,
        memory_state,
        oauth=oauth,
        oauth_state_capacity=1,
    )
    settings = Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )
    app = _auth_app(service=service, settings=settings, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        follow_redirects=False,
    ) as client:
        first = await client.get("/oauth/feishu/start")
        assert first.status_code == 302
        first_state = oauth.states[-1]
        first_cookie = client.cookies.get("__Host-xiaowei-oauth-state")

        rejected = await client.get("/oauth/feishu/start")
        rejected_state = oauth.states[-1]

    assert rejected.status_code == 503
    assert rejected.json() == {"error": {"code": "unavailable"}}
    assert rejected.headers.get_list("set-cookie") == []
    rendered = rejected.text + repr(dict(rejected.headers)) + caplog.text
    assert first_state not in rendered
    assert rejected_state not in rendered
    assert first_cookie is not None
    assert first_cookie not in rendered
    assert "state_digest" not in rendered
    assert "database" not in rendered.lower()


async def test_oauth_exchange_has_one_bounded_attempt_and_cancels_timeout(
    clock, memory_state
) -> None:
    class _HangingOAuth(_OAuth):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.cancelled = False

        async def exchange_code(
            self, *, code: str, redirect_uri: str
        ) -> FeishuOAuthIdentity:
            del code, redirect_uri
            self.calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    oauth = _HangingOAuth()
    principal = _principal()
    tokens = _tokens()
    service = WebAuthService(
        sessions=InMemoryWebSessionStore(clock=clock, state=memory_state),
        identities=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        oauth=oauth,
        public_origin="https://ops.example.test",
        oauth_state_ttl_seconds=300,
        session_ttl_seconds=3600,
        oauth_timeout_seconds=0.01,
        token_factory=lambda: next(tokens),
    )
    start = await service.start_login()

    with pytest.raises(WebOAuthUnavailableError):
        await service.complete_login(
            code="provider-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )

    assert oauth.calls == 1
    assert oauth.cancelled is True
    with pytest.raises(WebOAuthStateError):
        await service.complete_login(
            code="provider-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )
    assert oauth.calls == 1


async def test_unknown_identity_is_mapped_without_retaining_subject_context(
    clock, memory_state
) -> None:
    subject_ref = "external-sensitive-subject"
    service = _service(
        clock,
        memory_state,
        oauth=_OAuth(subject_ref=subject_ref),
    )
    start = await service.start_login()

    try:
        await service.complete_login(
            code="provider-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )
    except WebAuthenticationError as exc:
        rendered = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        assert subject_ref not in rendered
        assert subject_ref not in repr(exc.__dict__)
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("expected WebAuthenticationError")


@pytest.mark.parametrize(
    "authorization_url",
    [
        "https://feishu.example.test/authorize",
        "https://feishu.example.test/authorize?state=wrong-state-value-1234",
        "https://feishu.example.test/authorize\n",
        "https://feishu.example.test/authorize\t",
        (
            "https://feishu.example.test/authorize?"
            "state=state_value_first_1234567890&state=duplicate-state-value-1234"
        ),
    ],
)
async def test_authorization_redirect_must_bind_the_exact_generated_state_once(
    clock, memory_state, authorization_url: str
) -> None:
    service = _service(
        clock,
        memory_state,
        oauth=_OAuth(authorization_url=authorization_url),
    )

    with pytest.raises(WebOAuthCodeError):
        await service.start_login()

    assert memory_state.oauth_states == {}


@pytest.mark.parametrize(
    "public_origin",
    [
        "https://ops.example.test/path",
        "https://ops.example.test?next=/app",
        "https://ops.example.test/#fragment",
        "https://ops.example.test\n",
        "https://ops.example.test\t",
    ],
)
def test_public_origin_is_an_origin_not_a_url_path(
    clock, memory_state, public_origin: str
) -> None:
    principal = _principal()
    with pytest.raises(ValueError, match="web public origin must use https"):
        WebAuthService(
            sessions=InMemoryWebSessionStore(clock=clock, state=memory_state),
            identities=StaticFeishuIdentityDirectory(
                principals={principal.subject_ref: principal}
            ),
            oauth=_OAuth(),
            public_origin=public_origin,
            oauth_state_ttl_seconds=300,
            session_ttl_seconds=3600,
        )


async def test_oversized_logout_is_rejected_before_auth_state_changes(
    clock, memory_state
) -> None:
    oauth = _OAuth()
    service = _service(clock, memory_state, oauth=oauth)
    settings = Settings(
        environment_id="dev",
        api_request_body_limit_bytes=32,
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )
    app = _auth_app(service=service, settings=settings, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        follow_redirects=False,
    ) as client:
        start = await client.get("/oauth/feishu/start")
        assert start.status_code == 302
        callback = await client.get(
            "/oauth/feishu/callback",
            params={"code": "provider-code", "state": oauth.states[-1]},
        )
        assert callback.status_code == 302
        before = await client.get("/app/api/me")
        csrf = before.json()["csrf_token"]

        rejected = await client.post(
            "/app/api/logout",
            content=b'{"padding":"' + b"x" * 64 + b'"}',
            headers={
                "content-type": "application/json",
                "origin": "https://ops.example.test",
                "x-csrf-token": csrf,
            },
        )

        assert rejected.status_code == 413
        assert rejected.json() == {"error": {"code": "payload_too_large"}}
        assert rejected.headers["cache-control"] == "no-store"
        assert (await client.get("/app/api/me")).status_code == 200


@pytest.mark.parametrize(
    ("malformed_header", "malformed_value"),
    (
        (b"origin", b"https://ops.example.test/\xff"),
        (b"x-csrf-token", b"\xff" * 64),
    ),
)
async def test_non_ascii_state_change_headers_are_forbidden_without_revocation(
    clock,
    memory_state,
    malformed_header: bytes,
    malformed_value: bytes,
) -> None:
    oauth = _OAuth()
    service = _service(clock, memory_state, oauth=oauth)
    settings = Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )
    app = _auth_app(service=service, settings=settings, clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        follow_redirects=False,
    ) as client:
        assert (await client.get("/oauth/feishu/start")).status_code == 302
        callback = await client.get(
            "/oauth/feishu/callback",
            params={"code": "provider-code", "state": oauth.states[-1]},
        )
        assert callback.status_code == 302
        me = await client.get("/app/api/me")
        csrf = me.json()["csrf_token"]
        headers = [
            (b"content-type", b"application/json"),
            (b"origin", b"https://ops.example.test"),
            (b"x-csrf-token", csrf.encode("ascii")),
        ]
        headers = [
            (name, malformed_value if name == malformed_header else value)
            for name, value in headers
        ]

        rejected = await client.post(
            "/app/api/logout",
            content=b"{}",
            headers=headers,
        )

        assert rejected.status_code == 403
        assert rejected.json() == {"error": {"code": "forbidden"}}
        assert (await client.get("/app/api/me")).status_code == 200


async def test_rejected_web_request_log_uses_only_closed_fields(
    clock, memory_state, caplog: pytest.LogCaptureFixture
) -> None:
    oauth = _OAuth()
    service = _service(clock, memory_state, oauth=oauth)
    settings = Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )
    app = _auth_app(service=service, settings=settings, clock=clock)
    code = "provider-" + "sensitive-code"
    state = "state_" + "sensitive_value_1234567890"
    cookie = "cookie_" + "sensitive_value_1234567890"
    bad_host = "host-sensitive.example.test"
    bad_origin = "https://origin-sensitive.example.test"
    task_id = "task-sensitive-path-parameter"
    with caplog.at_level(logging.INFO, logger="xiaowei_agent.interfaces.web_app"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://ops.example.test",
            follow_redirects=False,
        ) as client:
            response = await client.get(
                "/oauth/feishu/callback",
                params={"code": code, "state": state},
                headers={
                    "host": bad_host,
                    "origin": bad_origin,
                    "cookie": f"__Host-xiaowei-oauth-state={cookie}",
                    "x-trace-id": "e" * 32,
                },
            )
            path_response = await client.get(f"/app/tasks/{task_id}")

    assert response.status_code == 403
    assert path_response.status_code == 401
    records = [
        record
        for record in caplog.records
        if record.name == "xiaowei_agent.interfaces.web_app"
    ]
    assert [record.route_class for record in records] == [
        "oauth_callback",
        "task_shell",
    ]
    assert [record.outcome for record in records] == ["rejected", "rejected"]
    rendered = caplog.text + repr([record.__dict__ for record in records])
    for sensitive in (code, state, cookie, bad_host, bad_origin, task_id):
        assert sensitive not in rendered


async def test_provider_failure_log_omits_provider_body_and_opaque_identity(
    clock, memory_state, caplog: pytest.LogCaptureFixture
) -> None:
    provider_body = (
        '{"open_id":"ou_sensitive","access_'
        + 'token":"provider-sensitive-value"}'
    )
    oauth = _OAuth(exchange_error=RuntimeError(provider_body))
    service = _service(clock, memory_state, oauth=oauth)
    settings = Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )
    app = _auth_app(service=service, settings=settings, clock=clock)
    code = "provider-" + "sensitive-code"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://ops.example.test",
        follow_redirects=False,
    ) as client:
        assert (await client.get("/oauth/feishu/start")).status_code == 302
        state = oauth.states[-1]
        state_cookie = client.cookies.get("__Host-xiaowei-oauth-state")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="xiaowei_agent.interfaces.web_app"):
            response = await client.get(
                "/oauth/feishu/callback",
                params={"code": code, "state": state},
            )

    assert response.status_code == 503
    records = [
        record
        for record in caplog.records
        if record.name == "xiaowei_agent.interfaces.web_app"
    ]
    assert len(records) == 1
    assert records[0].route_class == "oauth_callback"
    assert records[0].outcome == "failed"
    rendered = response.text + caplog.text + repr(records[0].__dict__)
    assert state_cookie is not None
    for sensitive in (provider_body, code, state, state_cookie, "ou_sensitive"):
        assert sensitive not in rendered


def test_web_stack_field_surface_has_no_execution_authority() -> None:
    names = {field.name for field in fields(WebStack)}
    assert not names & {
        "gateway",
        "runner",
        "tool_adapter",
        "xiaowei_runtime",
    }
    assert names == {
        "auth",
        "oauth_port",
        "membership",
        "runtime",
        "task_store",
        "channel_store",
        "web_session_store",
        "identity_directory",
        "task_access_service",
        "submission_service",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }


def test_built_web_process_has_exact_nonexecuting_module_surface() -> None:
    script = """
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity
from xiaowei_agent.interfaces.local_stack import build_postgres_web_stack

class OAuth:
    def authorization_url(self, *, state, redirect_uri):
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(self, *, code, redirect_uri):
        return FeishuOAuthIdentity(subject_ref="user-open-id")

class Membership:
    async def is_current_group_member(
        self, *, tenant_id, conversation_ref, subject_ref
    ):
        return True

async def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        postgres = root / "postgres-credential"
        postgres.write_text("local-" + "fixture", encoding="utf-8")
        identity = root / "identities.json"
        identity.write_text(json.dumps({
            "version": 1,
            "tenant_id": "dev-local",
            "environment_id": "dev",
            "entries": [],
        }), encoding="utf-8")
        stack = await build_postgres_web_stack(
            settings=Settings(
                environment_id="dev",
                postgres_password_file=str(postgres),
                web_app_enabled=True,
                feishu_oauth_enabled=True,
                feishu_app_id="app",
                feishu_app_secret_file=str(root / "missing"),
                feishu_identity_file=str(identity),
                web_detail_base_url="https://ops.example.test",
            ),
            oauth=OAuth(),
            membership=Membership(),
        )
        await stack.aclose()

asyncio.run(main())
assert "lark_oapi" not in sys.modules
"""
    loaded = _loaded_xiaowei_modules_after(script)
    internal_api_only = {
        "xiaowei_agent.interfaces.api",
        "xiaowei_agent.interfaces.auth",
        "xiaowei_agent.interfaces.body_limit",
        "xiaowei_agent.interfaces.http_models",
        "xiaowei_agent.log",
        "xiaowei_agent.trace",
    }
    web_only = {
        "xiaowei_agent.application.channel_access",
        "xiaowei_agent.application.channel_submission",
        "xiaowei_agent.interfaces.feishu_identity",
        "xiaowei_agent.interfaces.web_auth",
    }
    assert loaded == (
        set(_TASK_VIEW_PROCESS_ALLOWED_MODULES) - internal_api_only
    ) | web_only

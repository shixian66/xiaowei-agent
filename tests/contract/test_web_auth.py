"""飞书 OAuth 与浏览器 session 编排的离线契约。"""

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    WebMode,
)
from xiaowei_agent.interfaces import web_auth as web_auth_module
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.web_auth import (
    FeishuOAuthCodeError,
    FeishuOAuthIdentity,
    OAuthStart,
    WebAuthenticationError,
    WebAuthService,
    WebCsrfError,
    WebOAuthCodeError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
    WebOriginError,
)
from xiaowei_agent.persistence.fake import InMemoryWebSessionStore


def test_real_oauth_inner_deadline_is_strictly_inside_the_service_deadline() -> None:
    assert web_auth_module.FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS == 5.0
    assert web_auth_module.FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS == 6.0
    assert (
        0
        < web_auth_module.FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS
        < web_auth_module.FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS
    )


class _RecordingOAuth:
    def __init__(
        self,
        *,
        subject_ref: str = "subject-alice",
        authorization_url: str = "https://feishu.example.test/authorize",
        code_error: bool = False,
    ) -> None:
        self.subject_ref = subject_ref
        self.authorization_url_value = authorization_url
        self.code_error = code_error
        self.authorization_calls: list[tuple[str, str]] = []
        self.exchange_calls: list[tuple[str, str]] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        self.authorization_calls.append((state, redirect_uri))
        return f"{self.authorization_url_value}?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        self.exchange_calls.append((code, redirect_uri))
        if self.code_error:
            raise FeishuOAuthCodeError
        return FeishuOAuthIdentity(subject_ref=self.subject_ref)


def _principal(subject_ref: str = "subject-alice") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref=subject_ref,
        permissions=frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    )


def _token_factory(*values: str):
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


def test_oauth_identity_reference_is_bounded_at_the_provider_boundary() -> None:
    with pytest.raises(ValidationError):
        FeishuOAuthIdentity(subject_ref="x" * 257)


@pytest.mark.parametrize("control", ["\u0080", "\u0085", "\u009f"])
def test_oauth_identity_reference_rejects_c1_controls(control: str) -> None:
    with pytest.raises(ValidationError):
        FeishuOAuthIdentity(subject_ref=f"subject{control}alice")


def test_oauth_identity_reference_accepts_printable_non_ascii() -> None:
    assert FeishuOAuthIdentity(subject_ref="飞书用户").subject_ref == "飞书用户"


def _service(
    *,
    clock,
    memory_state,
    oauth: _RecordingOAuth | None = None,
    identities: StaticFeishuIdentityDirectory | None = None,
    tokens: tuple[str, ...] = ("state_value_1234567890", "session_value_1234567890"),
    oauth_state_capacity: int = 1024,
) -> tuple[WebAuthService, _RecordingOAuth, InMemoryWebSessionStore]:
    oauth_port = _RecordingOAuth() if oauth is None else oauth
    sessions = InMemoryWebSessionStore(
        clock=clock,
        state=memory_state,
        oauth_state_capacity=oauth_state_capacity,
    )
    principal = _principal()
    directory = identities or StaticFeishuIdentityDirectory(
        principals={principal.subject_ref: principal}
    )
    return (
        WebAuthService(
            sessions=sessions,
            identities=directory,
            oauth=oauth_port,
            public_origin="https://ops.example.test",
            mode=WebMode.HTTPS,
            oauth_state_ttl_seconds=300,
            session_ttl_seconds=3600,
            token_factory=_token_factory(*tokens),
        ),
        oauth_port,
        sessions,
    )


async def test_login_start_persists_only_digest_and_uses_trusted_callback(
    clock, memory_state
) -> None:
    service, oauth, _ = _service(clock=clock, memory_state=memory_state)

    start = await service.start_login()

    assert isinstance(start, OAuthStart)
    assert start.state_cookie == "state_value_1234567890"
    assert start.max_age_seconds == 300
    assert start.authorization_url.startswith("https://feishu.example.test/")
    assert oauth.authorization_calls == [
        (
            "state_value_1234567890",
            "https://ops.example.test/oauth/feishu/callback",
        )
    ]
    assert "state_value_1234567890" not in memory_state.oauth_states
    assert len(memory_state.oauth_states) == 1
    digest = next(iter(memory_state.oauth_states))
    assert len(digest) == 64


async def test_login_start_maps_state_capacity_to_closed_unavailable_error(
    clock, memory_state
) -> None:
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth_state_capacity=1,
        tokens=("first_state_value_1234567890", "second_state_value_1234567890"),
    )
    await service.start_login()

    try:
        await service.start_login()
    except WebOAuthUnavailableError as exc:
        assert str(exc) == "oauth provider unavailable"
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("expected WebOAuthUnavailableError")


async def test_state_cookie_mismatch_does_not_burn_the_valid_state(
    clock, memory_state
) -> None:
    service, oauth, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login()

    with pytest.raises(WebOAuthStateError):
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie="different_state_1234567890",
            previous_session_cookie=None,
        )

    completed = await service.complete_login(
        code="valid-code",
        state=start.state_cookie,
        state_cookie=start.state_cookie,
        previous_session_cookie=None,
    )
    assert completed.principal.actor == "alice"
    assert len(oauth.exchange_calls) == 1


async def test_oauth_state_is_expired_once_and_replay_safe(clock, memory_state) -> None:
    service, oauth, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login()
    clock.advance(seconds=300)

    for _ in range(2):
        with pytest.raises(WebOAuthStateError):
            await service.complete_login(
                code="valid-code",
                state=start.state_cookie,
                state_cookie=start.state_cookie,
                previous_session_cookie=None,
            )
    assert oauth.exchange_calls == []


async def test_code_failure_consumes_state_before_provider_result(
    clock, memory_state
) -> None:
    oauth = _RecordingOAuth(code_error=True)
    service, _, _ = _service(clock=clock, memory_state=memory_state, oauth=oauth)
    start = await service.start_login()

    with pytest.raises(WebOAuthCodeError):
        await service.complete_login(
            code="invalid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )
    oauth.code_error = False
    with pytest.raises(WebOAuthStateError):
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )
    assert len(oauth.exchange_calls) == 1


async def test_unknown_identity_never_creates_a_session(clock, memory_state) -> None:
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth=_RecordingOAuth(subject_ref="unknown-subject"),
    )
    start = await service.start_login()

    with pytest.raises(WebAuthenticationError):
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )

    assert memory_state.web_sessions == {}


async def test_login_rotates_old_session_and_current_directory_is_authoritative(
    clock, memory_state
) -> None:
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        tokens=(
            "state_value_first_1234567890",
            "session_value_first_1234567890",
            "state_value_second_1234567890",
            "session_value_second_1234567890",
        ),
    )
    first_start = await service.start_login()
    first = await service.complete_login(
        code="valid-code",
        state=first_start.state_cookie,
        state_cookie=first_start.state_cookie,
        previous_session_cookie=None,
    )
    second_start = await service.start_login()
    second = await service.complete_login(
        code="valid-code",
        state=second_start.state_cookie,
        state_cookie=second_start.state_cookie,
        previous_session_cookie=first.session_cookie,
    )

    with pytest.raises(WebAuthenticationError):
        await service.authenticate(session_cookie=first.session_cookie)
    authenticated = await service.authenticate(session_cookie=second.session_cookie)
    assert authenticated.principal.actor == "alice"

    empty_directory = StaticFeishuIdentityDirectory(principals={})
    revoked_service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        identities=empty_directory,
        tokens=("unused_state_1234567890",),
    )
    with pytest.raises(WebAuthenticationError):
        await revoked_service.authenticate(session_cookie=second.session_cookie)


async def test_session_expiry_logout_origin_and_csrf_fail_closed(
    clock, memory_state
) -> None:
    service, _, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login()
    session = await service.complete_login(
        code="valid-code",
        state=start.state_cookie,
        state_cookie=start.state_cookie,
        previous_session_cookie=None,
    )
    authenticated = await service.authenticate(session_cookie=session.session_cookie)
    csrf = authenticated.csrf_token

    service.validate_state_change(
        session_cookie=session.session_cookie,
        origin="https://ops.example.test",
        csrf_token=csrf,
    )
    with pytest.raises(WebOriginError):
        service.validate_state_change(
            session_cookie=session.session_cookie,
            origin="https://evil.example.test",
            csrf_token=csrf,
        )
    with pytest.raises(WebCsrfError):
        service.validate_state_change(
            session_cookie=session.session_cookie,
            origin="https://ops.example.test",
            csrf_token="wrong_csrf_value_" + "1234567890",
        )

    await service.logout(session_cookie=session.session_cookie)
    with pytest.raises(WebAuthenticationError):
        await service.authenticate(session_cookie=session.session_cookie)

    second_service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        tokens=("new_state_value_1234567890", "new_session_value_1234567890"),
    )
    second_start = await second_service.start_login()
    second = await second_service.complete_login(
        code="valid-code",
        state=second_start.state_cookie,
        state_cookie=second_start.state_cookie,
        previous_session_cookie=None,
    )
    clock.advance(seconds=3600)
    with pytest.raises(WebAuthenticationError):
        await second_service.authenticate(session_cookie=second.session_cookie)


async def test_untrusted_authorization_redirect_is_rejected(clock, memory_state) -> None:
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth=_RecordingOAuth(authorization_url="http://feishu.example.test/authorize"),
    )

    with pytest.raises(WebOAuthCodeError):
        await service.start_login()


async def test_printable_non_ascii_authorization_path_remains_supported(
    clock, memory_state
) -> None:
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth=_RecordingOAuth(
            authorization_url="https://feishu.example.test/授权",
        ),
    )

    start = await service.start_login()

    assert start.authorization_url.startswith("https://feishu.example.test/授权?")


async def test_printable_non_ascii_oauth_code_reaches_provider(
    clock, memory_state
) -> None:
    service, oauth, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login()

    completed = await service.complete_login(
        code="一次性授权码",
        state=start.state_cookie,
        state_cookie=start.state_cookie,
        previous_session_cookie=None,
    )

    assert completed.principal.actor == "alice"
    assert oauth.exchange_calls == [
        ("一次性授权码", "https://ops.example.test/oauth/feishu/callback")
    ]

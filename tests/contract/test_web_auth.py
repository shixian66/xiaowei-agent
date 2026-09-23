"""飞书 OAuth 与浏览器 session 编排的离线契约。"""

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    ActivationStatus,
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    ProductRole,
    WebMode,
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.interfaces import web_auth as web_auth_module
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityUnavailableError,
    StaticFeishuIdentityDirectory,
    WebIdentityDirectory,
    WebIdentityResolution,
)
from xiaowei_agent.interfaces.web_auth import (
    FeishuOAuthCodeError,
    FeishuOAuthIdentity,
    OAuthStart,
    WebActivationPendingError,
    WebAuthenticationError,
    WebAuthService,
    WebCsrfError,
    WebDestinationNotAvailableError,
    WebOAuthCodeError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
    WebOriginError,
)
from xiaowei_agent.persistence.activation import ActivationCapacityError
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


class _ActivationRequests:
    def __init__(self, *, capacity: bool = False) -> None:
        self.capacity = capacity
        self.subjects: list[str] = []
        self.intents: list[WebReturnIntent] = []
        self.requests: dict[str, object] = {}

    async def request_web(self, *, subject_ref: str, return_intent):
        self.subjects.append(subject_ref)
        self.intents.append(return_intent)
        if self.capacity:
            raise ActivationCapacityError
        request = SimpleNamespace(
            request_id=f"activation-{len(self.subjects)}",
            subject_ref=subject_ref,
            status=ActivationStatus.PENDING,
            return_intent=return_intent,
        )
        self.requests[request.request_id] = request
        return request

    async def resume_web(self, *, request_id: str, subject_ref: str):
        request = self.requests.get(request_id)
        if request is None or request.subject_ref != subject_ref:
            from xiaowei_agent.application.identity_activation import (
                ActivationResumeUnavailableError,
            )

            raise ActivationResumeUnavailableError
        return request


class _UnavailableIdentityDirectory:
    async def resolve_for_web(self, *, subject_ref: str) -> WebIdentityResolution:
        del subject_ref
        raise FeishuIdentityUnavailableError("feishu identity unavailable")


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


_WORKBENCH_INTENT = WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)


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
    identities: WebIdentityDirectory | None = None,
    tokens: tuple[str, ...] = ("state_value_1234567890", "session_value_1234567890"),
    oauth_state_capacity: int = 1024,
    activations: _ActivationRequests | None = None,
    role: ProductRole = ProductRole.OPERATOR,
) -> tuple[WebAuthService, _RecordingOAuth, InMemoryWebSessionStore]:
    oauth_port = _RecordingOAuth() if oauth is None else oauth
    sessions = InMemoryWebSessionStore(
        clock=clock,
        state=memory_state,
        oauth_state_capacity=oauth_state_capacity,
    )
    principal = _principal()
    directory = identities or StaticFeishuIdentityDirectory(
        principals={principal.subject_ref: principal},
        web_roles={principal.subject_ref: role},
    )
    return (
        WebAuthService(
            sessions=sessions,
            identities=directory,
            activations=activations or _ActivationRequests(),
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

    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

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
    await service.start_login(return_intent=_WORKBENCH_INTENT)

    try:
        await service.start_login(return_intent=_WORKBENCH_INTENT)
    except WebOAuthUnavailableError as exc:
        assert str(exc) == "oauth provider unavailable"
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("expected WebOAuthUnavailableError")


@pytest.mark.parametrize(
    ("role", "intent", "allowed"),
    (
        (
            ProductRole.ADMIN,
            WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
            True,
        ),
        (
            ProductRole.ADMIN,
            WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER),
            True,
        ),
        (
            ProductRole.OPERATOR,
            WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
            True,
        ),
        (
            ProductRole.OPERATOR,
            WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER),
            False,
        ),
        (
            ProductRole.USER,
            WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
            False,
        ),
        (
            ProductRole.USER,
            WebReturnIntent(
                kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
                task_id="task-1",
            ),
            True,
        ),
        (
            ProductRole.USER,
            WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER),
            False,
        ),
    ),
)
async def test_role_destination_matrix_is_enforced_before_session_rotation(
    clock,
    memory_state,
    role: ProductRole,
    intent: WebReturnIntent,
    allowed: bool,
) -> None:
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        role=role,
    )
    start = await service.start_login(return_intent=intent)

    if not allowed:
        with pytest.raises(WebDestinationNotAvailableError):
            await service.complete_login(
                code="valid-code",
                state=start.state_cookie,
                state_cookie=start.state_cookie,
                previous_session_cookie=None,
            )
        assert memory_state.web_sessions == {}
        return

    issued = await service.complete_login(
        code="valid-code",
        state=start.state_cookie,
        state_cookie=start.state_cookie,
        previous_session_cookie=None,
    )
    assert issued.role is role
    assert issued.return_intent == intent
    assert len(memory_state.web_sessions) == 1


async def test_state_cookie_mismatch_does_not_burn_the_valid_state(
    clock, memory_state
) -> None:
    service, oauth, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

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
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)
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
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

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
    activations = _ActivationRequests()
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth=_RecordingOAuth(subject_ref="unknown-subject"),
        activations=activations,
    )
    original = WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="task-1",
    )
    start = await service.start_login(return_intent=original)

    with pytest.raises(WebActivationPendingError) as caught:
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )

    assert memory_state.web_sessions == {}
    assert activations.subjects == ["unknown-subject"]
    assert activations.intents == [original]
    assert caught.value.request_id == "activation-1"
    assert caught.value.status is ActivationStatus.PENDING


async def test_activation_status_requires_the_same_subject_and_still_issues_no_session(
    clock, memory_state
) -> None:
    activations = _ActivationRequests()
    request = await activations.request_web(
        subject_ref="subject-alice",
        return_intent=_WORKBENCH_INTENT,
    )
    intent = WebReturnIntent(
        kind=WebReturnIntentKind.ACTIVATION_STATUS,
        request_id=request.request_id,
    )
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth=_RecordingOAuth(subject_ref="other-subject"),
        activations=activations,
    )
    start = await service.start_login(return_intent=intent)

    with pytest.raises(WebDestinationNotAvailableError):
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )
    assert memory_state.web_sessions == {}


async def test_approved_activation_restores_the_original_intent_and_role_gate(
    clock, memory_state
) -> None:
    activations = _ActivationRequests()
    original = WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="task-1",
    )
    request = await activations.request_web(
        subject_ref="subject-alice",
        return_intent=original,
    )
    request.status = ActivationStatus.APPROVED
    status_intent = WebReturnIntent(
        kind=WebReturnIntentKind.ACTIVATION_STATUS,
        request_id=request.request_id,
    )
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        activations=activations,
        role=ProductRole.USER,
    )
    start = await service.start_login(return_intent=status_intent)

    issued = await service.complete_login(
        code="valid-code",
        state=start.state_cookie,
        state_cookie=start.state_cookie,
        previous_session_cookie=None,
    )

    assert issued.return_intent == original
    assert issued.role is ProductRole.USER
    assert len(memory_state.web_sessions) == 1


async def test_activation_capacity_maps_to_unavailable_without_a_session(
    clock, memory_state
) -> None:
    activations = _ActivationRequests(capacity=True)
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        oauth=_RecordingOAuth(subject_ref="unknown-subject"),
        activations=activations,
    )
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

    with pytest.raises(WebOAuthUnavailableError):
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )

    assert memory_state.web_sessions == {}
    assert activations.subjects == ["unknown-subject"]


async def test_bound_but_unavailable_identity_never_reenters_activation(
    clock, memory_state
) -> None:
    activations = _ActivationRequests()
    service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        identities=_UnavailableIdentityDirectory(),
        activations=activations,
    )
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

    with pytest.raises(WebAuthenticationError):
        await service.complete_login(
            code="valid-code",
            state=start.state_cookie,
            state_cookie=start.state_cookie,
            previous_session_cookie=None,
        )

    assert memory_state.web_sessions == {}
    assert activations.subjects == []


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
    first_start = await service.start_login(return_intent=_WORKBENCH_INTENT)
    first = await service.complete_login(
        code="valid-code",
        state=first_start.state_cookie,
        state_cookie=first_start.state_cookie,
        previous_session_cookie=None,
    )
    second_start = await service.start_login(return_intent=_WORKBENCH_INTENT)
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


async def test_current_session_rejects_an_identity_that_becomes_unavailable(
    clock, memory_state
) -> None:
    service, _, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)
    session = await service.complete_login(
        code="valid-code",
        state=start.state_cookie,
        state_cookie=start.state_cookie,
        previous_session_cookie=None,
    )
    unavailable_service, _, _ = _service(
        clock=clock,
        memory_state=memory_state,
        identities=_UnavailableIdentityDirectory(),
        tokens=("unused_state_value_1234567890",),
    )

    with pytest.raises(WebAuthenticationError):
        await unavailable_service.authenticate(session_cookie=session.session_cookie)


async def test_session_expiry_logout_origin_and_csrf_fail_closed(
    clock, memory_state
) -> None:
    service, _, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)
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
    second_start = await second_service.start_login(return_intent=_WORKBENCH_INTENT)
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
        await service.start_login(return_intent=_WORKBENCH_INTENT)


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

    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

    assert start.authorization_url.startswith("https://feishu.example.test/授权?")


async def test_printable_non_ascii_oauth_code_reaches_provider(
    clock, memory_state
) -> None:
    service, oauth, _ = _service(clock=clock, memory_state=memory_state)
    start = await service.start_login(return_intent=_WORKBENCH_INTENT)

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


# --------------------------------------------------------------------------
# 两条认证来源共用一张 web_sessions 表：隔离必须是双向的
# --------------------------------------------------------------------------


async def _session_in(
    sessions: InMemoryWebSessionStore, cookie: str, *, auth_source: IdentitySource
) -> None:
    """在共享的 web_sessions 表里放一条有效 session，只有签发来源不同。"""
    from xiaowei_agent.interfaces.web_auth import web_origin_digest, web_session_digest
    from xiaowei_agent.persistence.local_admin import LOCAL_ADMIN_SUBJECT_REF
    from xiaowei_agent.persistence.web_session import RotateWebSessionCommand

    await sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest=web_session_digest(cookie),
            subject_ref=LOCAL_ADMIN_SUBJECT_REF,
            ttl_seconds=3600,
            auth_source=auth_source,
            public_origin_digest=web_origin_digest("https://ops.example.test"),
        )
    )


async def test_a_local_admin_session_is_not_accepted_as_a_feishu_session(
    clock, memory_state
) -> None:
    """本地管理员那侧已有反向校验，飞书这侧必须对称。

    两条路径写的是同一张表、同一套 digest 域，所以"能查到 session"根本不蕴含
    "这条 session 是我签发的"。缺了这一半，只要身份目录里恰好存在一条能解析
    ``local-admin`` 的条目，一张本地管理员 cookie 就会被当成飞书身份放行。
    """
    from xiaowei_agent.persistence.local_admin import LOCAL_ADMIN_SUBJECT_REF

    cookie = "local_admin_cookie_1234567890"
    service, _, sessions = _service(
        clock=clock,
        memory_state=memory_state,
        identities=StaticFeishuIdentityDirectory(
            principals={LOCAL_ADMIN_SUBJECT_REF: _principal(LOCAL_ADMIN_SUBJECT_REF)},
            web_roles={LOCAL_ADMIN_SUBJECT_REF: ProductRole.OPERATOR},
        ),
    )
    await _session_in(sessions, cookie, auth_source=IdentitySource.LOCAL_ADMIN)

    with pytest.raises(WebAuthenticationError) as caught:
        await service.authenticate(session_cookie=cookie)

    # 本模块一贯的约束：认证失败不携带上游异常上下文，免得把主体引用带进 traceback。
    assert caught.value.__context__ is None


async def test_a_feishu_session_of_the_same_shape_is_still_accepted(
    clock, memory_state
) -> None:
    """正对照：拒绝必须来自 ``auth_source``，而不是这条用例本来就查不到 session。"""
    from xiaowei_agent.persistence.local_admin import LOCAL_ADMIN_SUBJECT_REF

    cookie = "feishu_issued_cookie_1234567890"
    service, _, sessions = _service(
        clock=clock,
        memory_state=memory_state,
        identities=StaticFeishuIdentityDirectory(
            principals={LOCAL_ADMIN_SUBJECT_REF: _principal(LOCAL_ADMIN_SUBJECT_REF)},
            web_roles={LOCAL_ADMIN_SUBJECT_REF: ProductRole.OPERATOR},
        ),
    )
    await _session_in(sessions, cookie, auth_source=IdentitySource.FEISHU)

    authenticated = await service.authenticate(session_cookie=cookie)

    assert authenticated.principal.subject_ref == LOCAL_ADMIN_SUBJECT_REF


async def test_a_feishu_session_is_not_accepted_as_a_local_admin_session(
    clock, memory_state
) -> None:
    """反方向的同一条不变量；两条一起写，避免以后只修一侧。"""
    from xiaowei_agent.interfaces.local_admin_auth import (
        LocalAdminAuthenticationError,
        LocalAdminAuthService,
    )
    from xiaowei_agent.interfaces.web_auth import web_origin_digest
    from xiaowei_agent.persistence.fake import InMemoryLocalAdminStore

    cookie = "feishu_issued_cookie_1234567890"
    sessions = InMemoryWebSessionStore(clock=clock, state=memory_state)
    await _session_in(sessions, cookie, auth_source=IdentitySource.FEISHU)
    service = LocalAdminAuthService(
        admins=InMemoryLocalAdminStore(clock=clock, state=memory_state),
        sessions=sessions,
        public_origin_digest=web_origin_digest("https://ops.example.test"),
        session_ttl_seconds=3600,
    )

    with pytest.raises(LocalAdminAuthenticationError):
        await service.authenticate(session_cookie=cookie)

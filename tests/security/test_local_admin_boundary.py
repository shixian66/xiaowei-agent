"""本地管理员的认证边界：固定主体、口令不外泄、origin 绑定、改密即全体登出。"""

from typing import Any

import pytest

from xiaowei_agent.contracts import ChannelPermission, IdentitySource
from xiaowei_agent.interfaces.local_admin_auth import (
    LOCAL_ADMIN_PRINCIPAL,
    LocalAdminAuthenticationError,
    LocalAdminAuthService,
    hash_password,
)
from xiaowei_agent.interfaces.web_auth import web_origin_digest, web_session_digest
from xiaowei_agent.persistence.fake import (
    InMemoryLocalAdminStore,
    InMemoryWebSessionStore,
)
from xiaowei_agent.persistence.web_session import (
    RotateWebSessionCommand,
    WebSessionLookup,
    WebSessionNotFoundError,
)

pytestmark = pytest.mark.security

_ORIGIN = "http://127.0.0.1:8080"
_OTHER_ORIGIN = "https://sso.example.test"
_INITIAL_PASSWORD = "admin" + "-initial"


def _tokens(count: int = 16) -> Any:
    values = iter(f"cookie-{index:02d}-{'x' * 32}" for index in range(count))
    return lambda: next(values)


async def _service(clock: Any, memory_state: Any, *, origin: str = _ORIGIN) -> Any:
    admins = InMemoryLocalAdminStore(clock=clock, state=memory_state)
    sessions = InMemoryWebSessionStore(clock=clock, state=memory_state)
    await admins.seed_if_absent(password_hash=hash_password(_INITIAL_PASSWORD))
    service = LocalAdminAuthService(
        admins=admins,
        sessions=sessions,
        public_origin_digest=web_origin_digest(origin),
        session_ttl_seconds=3600,
        token_factory=_tokens(),
    )
    return service, admins, sessions


async def test_local_admin_principal_is_fixed(clock, memory_state) -> None:
    service, _, _ = await _service(clock, memory_state)
    issued = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )

    assert issued.principal is LOCAL_ADMIN_PRINCIPAL
    assert LOCAL_ADMIN_PRINCIPAL.tenant_id == "dev-local"
    assert LOCAL_ADMIN_PRINCIPAL.environment_id == "dev"
    assert LOCAL_ADMIN_PRINCIPAL.actor == "admin"
    assert LOCAL_ADMIN_PRINCIPAL.source is IdentitySource.LOCAL_ADMIN
    assert LOCAL_ADMIN_PRINCIPAL.permissions == frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
            ChannelPermission.ADMIN_ALL_SAFE_TASKS,
        }
    )


async def test_password_hash_never_appears_in_any_returned_payload(
    clock, memory_state
) -> None:
    service, admins, _ = await _service(clock, memory_state)
    stored = (await admins.get()).password_hash

    issued = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )
    authenticated = await service.authenticate(session_cookie=issued.session_cookie)

    for rendered in (repr(issued), repr(authenticated), repr(issued.principal)):
        assert stored not in rendered
        assert _INITIAL_PASSWORD not in rendered
        # cookie 与 csrf token 同样不得出现在 repr 里。
        assert issued.session_cookie not in rendered
        assert authenticated.csrf_token not in rendered


async def test_session_is_bound_to_the_public_origin_that_created_it(
    clock, memory_state
) -> None:
    service, admins, sessions = await _service(clock, memory_state)
    issued = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )

    other = LocalAdminAuthService(
        admins=admins,
        sessions=sessions,
        public_origin_digest=web_origin_digest(_OTHER_ORIGIN),
        session_ttl_seconds=3600,
        token_factory=_tokens(),
    )
    with pytest.raises(LocalAdminAuthenticationError):
        await other.authenticate(session_cookie=issued.session_cookie)
    assert await service.authenticate(session_cookie=issued.session_cookie)


async def test_change_password_revokes_every_other_local_admin_session(
    clock, memory_state
) -> None:
    service, admins, _sessions = await _service(clock, memory_state)
    first = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )
    second = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )
    assert first.session_cookie != second.session_cookie

    new_password = "rotated" + "-secret"
    rotated = await service.change_password(
        session_cookie=second.session_cookie,
        current=_INITIAL_PASSWORD,
        new=new_password,
    )

    for stale in (first.session_cookie, second.session_cookie):
        with pytest.raises(LocalAdminAuthenticationError):
            await service.authenticate(session_cookie=stale)
    assert await service.authenticate(session_cookie=rotated.session_cookie)
    assert (await admins.get()).must_change_password is False
    # 口令确实换了：旧口令不再能登录，新口令可以。
    with pytest.raises(LocalAdminAuthenticationError):
        await service.login(
            password=_INITIAL_PASSWORD, previous_session_cookie=None
        )
    assert await service.login(password=new_password, previous_session_cookie=None)


async def test_change_password_is_atomic_across_admin_and_sessions(
    clock, memory_state
) -> None:
    """反例守护：撤销必须先于插入，否则新 session 会被自己那一批撤掉。"""
    service, _, sessions = await _service(clock, memory_state)
    live = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )
    rotated = await service.change_password(
        session_cookie=live.session_cookie,
        current=_INITIAL_PASSWORD,
        new="rotated" + "-secret",
    )

    session = await sessions.get_session(
        lookup=WebSessionLookup(
            session_digest=web_session_digest(rotated.session_cookie),
            public_origin_digest=web_origin_digest(_ORIGIN),
        )
    )
    assert session.revoked_at is None
    assert session.auth_source is IdentitySource.LOCAL_ADMIN


async def test_seed_is_idempotent_and_forces_a_password_change(
    clock, memory_state
) -> None:
    service, admins, _ = await _service(clock, memory_state)
    first_hash = (await admins.get()).password_hash

    assert await admins.seed_if_absent(password_hash=hash_password("other")) is False
    assert (await admins.get()).password_hash == first_hash
    assert (await admins.get()).must_change_password is True

    issued = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )
    assert (
        await service.authenticate(session_cookie=issued.session_cookie)
    ).must_change_password is True


async def test_a_feishu_session_cannot_authenticate_as_the_local_admin(
    clock, memory_state
) -> None:
    """同一张表里飞书 session 也有效，但不得走本地管理员这条路径。"""
    service, _, sessions = await _service(clock, memory_state)
    cookie = "feishu-cookie-" + "y" * 32
    await sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest=web_session_digest(cookie),
            subject_ref="subject-alice",
            auth_source=IdentitySource.FEISHU,
            public_origin_digest=web_origin_digest(_ORIGIN),
            ttl_seconds=3600,
        )
    )

    with pytest.raises(LocalAdminAuthenticationError):
        await service.authenticate(session_cookie=cookie)
    # 反例：这个 session 本身是有效的，被拒的原因只能是 auth_source。
    assert await sessions.get_session(
        lookup=WebSessionLookup(
            session_digest=web_session_digest(cookie),
            public_origin_digest=web_origin_digest(_ORIGIN),
        )
    )


async def test_a_wrong_password_never_creates_a_session(clock, memory_state) -> None:
    service, _, _sessions = await _service(clock, memory_state)

    with pytest.raises(LocalAdminAuthenticationError):
        await service.login(
            password=_INITIAL_PASSWORD + "x", previous_session_cookie=None
        )
    assert memory_state.web_sessions == {}


async def test_logout_revokes_and_is_idempotent(clock, memory_state) -> None:
    service, _, sessions = await _service(clock, memory_state)
    issued = await service.login(
        password=_INITIAL_PASSWORD, previous_session_cookie=None
    )

    assert await service.logout(session_cookie=issued.session_cookie) is True
    assert await service.logout(session_cookie=issued.session_cookie) is False
    with pytest.raises(LocalAdminAuthenticationError):
        await service.authenticate(session_cookie=issued.session_cookie)
    with pytest.raises(WebSessionNotFoundError):
        await sessions.get_session(
            lookup=WebSessionLookup(
                session_digest=web_session_digest(issued.session_cookie),
                public_origin_digest=web_origin_digest(_ORIGIN),
            )
        )

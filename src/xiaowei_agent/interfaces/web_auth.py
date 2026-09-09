"""飞书 OAuth、浏览器 session 与 CSRF 的薄认证边界。"""

import asyncio
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol, TypeGuard
from urllib.parse import SplitResult, parse_qs, urlsplit

from pydantic import Field

from xiaowei_agent.contracts import AuthenticatedPrincipal, Contract, StrictStr
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityDirectory,
    FeishuIdentityNotFoundError,
)
from xiaowei_agent.persistence.web_session import (
    ConsumeOAuthStateCommand,
    IssueOAuthStateCommand,
    OAuthStateNotFoundError,
    RevokeWebSessionCommand,
    RotateWebSessionCommand,
    WebSessionLookup,
    WebSessionNotFoundError,
    WebSessionStore,
)

_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,512}")


class WebAuthenticationError(RuntimeError):
    """浏览器 session 不存在或当前身份目录已撤权。"""

    def __init__(self) -> None:
        super().__init__("web authentication failed")


class WebOAuthStateError(RuntimeError):
    """OAuth state 缺失、不匹配、过期或已消费。"""

    def __init__(self) -> None:
        super().__init__("oauth state invalid")


class WebOAuthCodeError(RuntimeError):
    """OAuth code 或供应商返回不满足本地身份契约。"""

    def __init__(self) -> None:
        super().__init__("oauth exchange failed")


class WebOAuthUnavailableError(RuntimeError):
    """OAuth 供应商暂时不可用。"""

    def __init__(self) -> None:
        super().__init__("oauth provider unavailable")


class WebOriginError(RuntimeError):
    """状态变更请求不是来自受信 Web origin。"""

    def __init__(self) -> None:
        super().__init__("web origin invalid")


class WebCsrfError(RuntimeError):
    """状态变更请求没有匹配当前 session 的 CSRF token。"""

    def __init__(self) -> None:
        super().__init__("csrf token invalid")


class FeishuOAuthCodeError(RuntimeError):
    """供应商明确拒绝一次 OAuth code。"""


class FeishuOAuthUnavailableError(RuntimeError):
    """供应商调用超时或暂时不可用。"""


class FeishuOAuthIdentity(Contract):
    """OAuth provider 返回后立即收窄的唯一身份事实。"""

    subject_ref: StrictStr = Field(max_length=256)


class FeishuOAuthPort(Protocol):
    """飞书 OAuth 的窄端口；M7 PR 6 只允许 fake 离线实现。"""

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """返回携带给定 state 与固定 callback 的 HTTPS 授权 URL。"""

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        """交换一次 code；不得返回 tenant、权限或 actor。"""


@dataclass(frozen=True)
class OAuthStart:
    authorization_url: str = field(repr=False)
    state_cookie: str = field(repr=False)
    max_age_seconds: int


@dataclass(frozen=True)
class IssuedWebSession:
    session_cookie: str = field(repr=False)
    principal: AuthenticatedPrincipal
    max_age_seconds: int


@dataclass(frozen=True)
class AuthenticatedWebSession:
    principal: AuthenticatedPrincipal
    csrf_token: str = field(repr=False)


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


def _secret_is_valid(value: str | None) -> TypeGuard[str]:
    return isinstance(value, str) and _SECRET_RE.fullmatch(value) is not None


def _digest(*, domain: str, secret: str) -> str:
    return sha256(f"{domain}:{secret}".encode()).hexdigest()


def _split_https_url(value: str) -> SplitResult | None:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if not (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    ):
        return None
    return parsed


def _public_origin_is_safe(value: str) -> bool:
    parsed = _split_https_url(value)
    return parsed is not None and parsed.path in {"", "/"} and not parsed.query


def _authorization_url_is_safe(value: str, *, expected_state: str) -> bool:
    if len(value) > 8192:
        return False
    parsed = _split_https_url(value)
    if parsed is None:
        return False
    try:
        states = parse_qs(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=100,
        ).get("state", [])
    except ValueError:
        return False
    return states == [expected_state]


class WebAuthService:
    """把 OAuth 身份重新映射为当前主体，并只持久化随机值摘要。"""

    def __init__(
        self,
        *,
        sessions: WebSessionStore,
        identities: FeishuIdentityDirectory,
        oauth: FeishuOAuthPort,
        public_origin: str,
        oauth_state_ttl_seconds: int,
        session_ttl_seconds: int,
        oauth_timeout_seconds: float = 5.0,
        token_factory: Callable[[], str] = _random_secret,
    ) -> None:
        if not _public_origin_is_safe(public_origin):
            raise ValueError("web public origin must use https")
        if not 0 < oauth_state_ttl_seconds <= 600:
            raise ValueError("oauth state ttl is out of range")
        if not 0 < session_ttl_seconds <= 86_400:
            raise ValueError("web session ttl is out of range")
        if isinstance(oauth_timeout_seconds, bool) or not (
            0 < oauth_timeout_seconds <= 30
        ):
            raise ValueError("oauth timeout is out of range")
        self._sessions = sessions
        self._identities = identities
        self._oauth = oauth
        self._public_origin = public_origin.rstrip("/")
        self._redirect_uri = f"{self._public_origin}/oauth/feishu/callback"
        self._oauth_state_ttl_seconds = oauth_state_ttl_seconds
        self._session_ttl_seconds = session_ttl_seconds
        self._oauth_timeout_seconds = oauth_timeout_seconds
        self._token_factory = token_factory

    def _new_secret(self) -> str:
        value = self._token_factory()
        if not _secret_is_valid(value):
            raise RuntimeError("secure random token generation failed")
        return value

    async def start_login(self) -> OAuthStart:
        """创建与浏览器临时 cookie 绑定的一次性 OAuth state。"""
        state = self._new_secret()
        authorization_url: str | None = None
        provider_failed = False
        try:
            authorization_url = self._oauth.authorization_url(
                state=state,
                redirect_uri=self._redirect_uri,
            )
        except Exception:
            provider_failed = True
        if provider_failed:
            raise WebOAuthUnavailableError
        if not isinstance(authorization_url, str) or not _authorization_url_is_safe(
            authorization_url,
            expected_state=state,
        ):
            raise WebOAuthCodeError
        await self._sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest=_digest(domain="oauth-state:v1", secret=state),
                ttl_seconds=self._oauth_state_ttl_seconds,
            )
        )
        return OAuthStart(
            authorization_url=authorization_url,
            state_cookie=state,
            max_age_seconds=self._oauth_state_ttl_seconds,
        )

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str | None,
        previous_session_cookie: str | None,
    ) -> IssuedWebSession:
        """消费同浏览器 state，重映射身份并原子轮换 session。"""
        if (
            not _secret_is_valid(state)
            or not _secret_is_valid(state_cookie)
            or not hmac.compare_digest(state, state_cookie)
        ):
            raise WebOAuthStateError
        state_missing = False
        try:
            await self._sessions.consume_oauth_state(
                command=ConsumeOAuthStateCommand(
                    state_digest=_digest(domain="oauth-state:v1", secret=state)
                )
            )
        except OAuthStateNotFoundError:
            state_missing = True
        if state_missing:
            raise WebOAuthStateError

        if not isinstance(code, str) or not code or len(code) > 2048:
            raise WebOAuthCodeError
        identity: FeishuOAuthIdentity | None = None
        provider_error: RuntimeError | None = None
        try:
            identity = await asyncio.wait_for(
                self._oauth.exchange_code(
                    code=code,
                    redirect_uri=self._redirect_uri,
                ),
                timeout=self._oauth_timeout_seconds,
            )
        except FeishuOAuthCodeError:
            provider_error = WebOAuthCodeError()
        except FeishuOAuthUnavailableError:
            provider_error = WebOAuthUnavailableError()
        except Exception:
            provider_error = WebOAuthUnavailableError()
        if provider_error is not None:
            raise provider_error
        if identity is None:
            raise WebOAuthUnavailableError
        identity_missing = False
        try:
            principal = self._identities.resolve(subject_ref=identity.subject_ref)
        except FeishuIdentityNotFoundError:
            identity_missing = True
        if identity_missing:
            raise WebAuthenticationError

        cookie = self._new_secret()
        previous_digest = (
            _digest(domain="web-session:v1", secret=previous_session_cookie)
            if _secret_is_valid(previous_session_cookie)
            else None
        )
        await self._sessions.rotate_session(
            command=RotateWebSessionCommand(
                session_digest=_digest(domain="web-session:v1", secret=cookie),
                previous_session_digest=previous_digest,
                subject_ref=identity.subject_ref,
                ttl_seconds=self._session_ttl_seconds,
            )
        )
        return IssuedWebSession(
            session_cookie=cookie,
            principal=principal,
            max_age_seconds=self._session_ttl_seconds,
        )

    async def authenticate(self, *, session_cookie: str | None) -> AuthenticatedWebSession:
        """按 cookie 摘要取 session，并从当前身份目录重建权限。"""
        if not _secret_is_valid(session_cookie):
            raise WebAuthenticationError
        authentication_failed = False
        try:
            session = await self._sessions.get_session(
                lookup=WebSessionLookup(
                    session_digest=_digest(
                        domain="web-session:v1", secret=session_cookie
                    )
                )
            )
            principal = self._identities.resolve(subject_ref=session.subject_ref)
        except (WebSessionNotFoundError, FeishuIdentityNotFoundError):
            authentication_failed = True
        if authentication_failed:
            raise WebAuthenticationError
        return AuthenticatedWebSession(
            principal=principal,
            csrf_token=_digest(domain="csrf:v1", secret=session_cookie),
        )

    def validate_state_change(
        self,
        *,
        session_cookie: str,
        origin: str | None,
        csrf_token: str | None,
    ) -> None:
        """校验严格同源和当前 session 派生的 CSRF token。"""
        if origin is None or not hmac.compare_digest(origin, self._public_origin):
            raise WebOriginError
        if not _secret_is_valid(session_cookie):
            raise WebCsrfError
        expected = _digest(domain="csrf:v1", secret=session_cookie)
        if not isinstance(csrf_token, str) or not hmac.compare_digest(
            csrf_token, expected
        ):
            raise WebCsrfError

    async def logout(self, *, session_cookie: str | None) -> None:
        """幂等撤销当前 session；无效 cookie 不泄露其存在性。"""
        if not _secret_is_valid(session_cookie):
            return
        await self._sessions.revoke_session(
            command=RevokeWebSessionCommand(
                session_digest=_digest(
                    domain="web-session:v1", secret=session_cookie
                )
            )
        )


__all__ = [
    "AuthenticatedWebSession",
    "FeishuOAuthCodeError",
    "FeishuOAuthIdentity",
    "FeishuOAuthPort",
    "FeishuOAuthUnavailableError",
    "IssuedWebSession",
    "OAuthStart",
    "WebAuthService",
    "WebAuthenticationError",
    "WebCsrfError",
    "WebOAuthCodeError",
    "WebOAuthStateError",
    "WebOAuthUnavailableError",
    "WebOriginError",
]

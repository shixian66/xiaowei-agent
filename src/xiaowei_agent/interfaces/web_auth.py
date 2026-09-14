"""飞书 OAuth、浏览器 session 与 CSRF 的薄认证边界。"""

import asyncio
import hmac
import re
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Annotated, Final, Protocol, TypeGuard
from urllib.parse import SplitResult, parse_qs, urlsplit

from pydantic import AfterValidator, Field

from xiaowei_agent.config import canonical_web_public_origin
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    Contract,
    IdentitySource,
    StrictStr,
    WebMode,
)
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityDirectory,
    FeishuIdentityNotFoundError,
)
from xiaowei_agent.persistence.web_session import (
    ConsumeOAuthStateCommand,
    IssueOAuthStateCommand,
    OAuthStateCapacityError,
    OAuthStateNotFoundError,
    RevokeWebSessionCommand,
    RotateWebSessionCommand,
    WebSessionLookup,
    WebSessionNotFoundError,
    WebSessionStore,
)

_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,512}")
FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS: Final[float] = 5.0
FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS: Final[float] = 6.0


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _without_control(value: str) -> str:
    if _has_control(value):
        raise ValueError("must not contain control characters")
    return value


_OAuthSubjectRef = Annotated[StrictStr, AfterValidator(_without_control)]


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
    """OAuth provider 的 ``open_id`` 进入本地后使用统一主体引用名。"""

    subject_ref: _OAuthSubjectRef = Field(max_length=256)


class FeishuOAuthPort(Protocol):
    """只接受受信 callback，并把官方闭集端点的结果收窄为 ``open_id``。"""

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """用代码内固定官方端点返回携带 state 与 callback 的 HTTPS URL。"""

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        """交换一次 code；只返回映射为 subject_ref 的 ``open_id`` 身份事实。"""


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


def _constant_time_ascii_equal(left: str | None, right: str | None) -> bool:
    """仅在双方都是 ASCII 时调用拒绝非 ASCII 文本的 compare_digest。"""
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.isascii()
        and right.isascii()
        and hmac.compare_digest(left, right)
    )


def _digest(*, domain: str, secret: str) -> str:
    return sha256(f"{domain}:{secret}".encode()).hexdigest()


def web_session_digest(session_cookie: str) -> str:
    """session cookie 的存储摘要。

    公开而不是私有：本地管理员登录写的是**同一张** ``web_sessions`` 表，两条
    认证路径必须用同一个域，否则同一个 cookie 会算出两个 digest，session 在
    另一条路径上直接查不到。域常量因此只能有一处。
    """
    return _digest(domain="web-session:v1", secret=session_cookie)


def web_csrf_token(session_cookie: str) -> str:
    """由当前 session cookie 派生的 CSRF token；同理只能有一处。"""
    return _digest(domain="csrf:v1", secret=session_cookie)


def web_origin_digest(public_origin: str) -> str:
    """public origin 的摘要；session 的 origin 绑定用它。"""
    return _digest(domain="web-origin:v1", secret=public_origin)


def _provider_https_url(value: str) -> SplitResult | None:
    """Provider 侧 URL 的 HTTPS 硬门。

    **不接受也不感知 ``mode``。** RI5 对本方 origin 放开 http 只是部署形态，
    Provider 授权/令牌端点在任何模式下都必须是 HTTPS——两者共用一个 helper，
    放宽本方就等于同时放宽了对方。
    """
    if _has_control(value):
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


def public_origin_is_safe(value: str, *, mode: WebMode) -> bool:
    """本方 public origin 是否满足该模式的形态要求。"""
    try:
        canonical_web_public_origin(value, mode=mode)
    except ValueError:
        return False
    return True


def _authorization_url_is_safe(value: str, *, expected_state: str) -> bool:
    if len(value) > 8192:
        return False
    parsed = _provider_https_url(value)
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
        mode: WebMode,
        oauth_state_ttl_seconds: int,
        session_ttl_seconds: int,
        oauth_timeout_seconds: float = FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS,
        token_factory: Callable[[], str] = _random_secret,
    ) -> None:
        if not public_origin_is_safe(public_origin, mode=mode):
            raise ValueError("web public origin does not match the web mode")
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
        self._mode = mode
        self._public_origin = public_origin.rstrip("/")
        # session 绑定到签发它的 origin：换模式或换 origin 之后，旧 session 的
        # digest 必然对不上，等价于全体登出。
        self._origin_digest = web_origin_digest(self._public_origin)
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
        capacity_reached = False
        try:
            await self._sessions.issue_oauth_state(
                command=IssueOAuthStateCommand(
                    state_digest=_digest(domain="oauth-state:v1", secret=state),
                    ttl_seconds=self._oauth_state_ttl_seconds,
                )
            )
        except OAuthStateCapacityError:
            capacity_reached = True
        if capacity_reached:
            raise WebOAuthUnavailableError
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
            or not _constant_time_ascii_equal(state, state_cookie)
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

        if (
            not isinstance(code, str)
            or not code
            or _has_control(code)
            or len(code) > 2048
        ):
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
            web_session_digest(previous_session_cookie)
            if _secret_is_valid(previous_session_cookie)
            else None
        )
        await self._sessions.rotate_session(
            command=RotateWebSessionCommand(
                session_digest=web_session_digest(cookie),
                previous_session_digest=previous_digest,
                subject_ref=identity.subject_ref,
                ttl_seconds=self._session_ttl_seconds,
                auth_source=IdentitySource.FEISHU,
                public_origin_digest=self._origin_digest,
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
                    session_digest=web_session_digest(session_cookie),
                    public_origin_digest=self._origin_digest,
                )
            )
            principal = self._identities.resolve(subject_ref=session.subject_ref)
        except (WebSessionNotFoundError, FeishuIdentityNotFoundError):
            authentication_failed = True
        if authentication_failed:
            raise WebAuthenticationError
        return AuthenticatedWebSession(
            principal=principal,
            csrf_token=web_csrf_token(session_cookie),
        )

    def validate_state_change(
        self,
        *,
        session_cookie: str,
        origin: str | None,
        csrf_token: str | None,
    ) -> None:
        """校验严格同源和当前 session 派生的 CSRF token。"""
        if not _constant_time_ascii_equal(origin, self._public_origin):
            raise WebOriginError
        if not _secret_is_valid(session_cookie):
            raise WebCsrfError
        expected = web_csrf_token(session_cookie)
        if not _secret_is_valid(csrf_token) or not _constant_time_ascii_equal(
            csrf_token, expected
        ):
            raise WebCsrfError

    async def logout(self, *, session_cookie: str | None) -> None:
        """幂等撤销当前 session；无效 cookie 不泄露其存在性。"""
        if not _secret_is_valid(session_cookie):
            return
        await self._sessions.revoke_session(
            command=RevokeWebSessionCommand(
                session_digest=web_session_digest(session_cookie)
            )
        )


__all__ = [
    "FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS",
    "FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS",
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
    "public_origin_is_safe",
    "web_csrf_token",
    "web_origin_digest",
    "web_session_digest",
]

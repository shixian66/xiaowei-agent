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
    CONTROLLED_PII_MAX_LENGTH,
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

    subject_ref: _OAuthSubjectRef = Field(max_length=CONTROLLED_PII_MAX_LENGTH)


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


OAUTH_LOGIN_STATE_DOMAIN: Final[str] = "oauth-state:v1"
OAUTH_TEST_STATE_DOMAIN: Final[str] = "oauth-conn-test:v1"
"""登录 state 与连接测试 state 的摘要域；**并排定义是刻意的**。

两者写的是同一张 ``oauth_states`` 表。域一旦相同，一次连接测试签发的 state 就能
被登录 callback 消费掉——那条路会 ``rotate_session`` 并下发 session cookie，于是
"点一下测试按钮"变成了"签发一个会话"。反方向同样致命：登录 state 被测试分支吃掉，
用户的登录会静默失败，而管理面记下一条并不存在的测试结果。

隔离必须双向，两个方向各有一条用例钉住。
"""


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


def validate_unauthenticated_origin(
    *, public_origin: str, origin: str | None, content_type: str | None
) -> None:
    """未登录状态变更的窄校验；**不派生也不检查 CSRF token**。

    首次登录必然没有 session cookie，而 CSRF token 是从 session cookie 派生的——
    让登录复用 :func:`validate_state_change` 等于要求用户先有 session 才能登录，
    第一次登录会稳定 403，整条闭环卡死在第一步。

    放宽的只有 CSRF 这一项，且仅限登录：``Origin`` 精确等于固定 public origin
    已足以挡住跨站表单提交（跨站脚本无法伪造 ``Origin``），而此刻浏览器里还不存在
    任何 ``SameSite=Lax`` 的凭据，因此没有可被 CSRF 滥用的既有权限。
    """
    if not _constant_time_ascii_equal(origin, public_origin):
        raise WebOriginError
    if (
        content_type is None
        or content_type.split(";")[0].strip().lower() != "application/json"
    ):
        raise WebCsrfError


def validate_state_change(
    *,
    public_origin: str,
    session_cookie: str,
    origin: str | None,
    csrf_token: str | None,
) -> None:
    """校验严格同源和当前 session 派生的 CSRF token。

    做成模块级函数：本地管理员路径没有 ``WebAuthService``（飞书可能整个不装配），
    但它写的是同一张 session 表、用同一个 CSRF 域，判定必须是同一份实现。
    """
    if not _constant_time_ascii_equal(origin, public_origin):
        raise WebOriginError
    if not _secret_is_valid(session_cookie):
        raise WebCsrfError
    expected = web_csrf_token(session_cookie)
    if not _secret_is_valid(csrf_token) or not _constant_time_ascii_equal(
        csrf_token, expected
    ):
        raise WebCsrfError


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
        """创建与浏览器临时 cookie 绑定的一次性登录 state。"""
        return await self._start(domain=OAUTH_LOGIN_STATE_DOMAIN)

    async def start_connection_test(self) -> OAuthStart:
        """签发一次**只能**被测试分支消费的 state。

        与登录共用授权 URL 与 redirect_uri——测的就是那条真实回调链路；换一个
        redirect_uri 等于测了一条生产环境里不存在的路径。
        """
        return await self._start(domain=OAUTH_TEST_STATE_DOMAIN)

    async def _start(self, *, domain: str) -> OAuthStart:
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
                    state_digest=_digest(domain=domain, secret=state),
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

    async def _consume_state(
        self, *, state: str, state_cookie: str | None, domain: str
    ) -> None:
        """一次性消费某个域下的 state；不属于这个域就当作不存在。

        先比 cookie 再查库：``state`` 来自 URL（攻击者可控），``state_cookie``
        来自浏览器。少了前一步，一个被诱导的回调就能替受害者消费掉 state。
        """
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
                    state_digest=_digest(domain=domain, secret=state)
                )
            )
        except OAuthStateNotFoundError:
            state_missing = True
        if state_missing:
            raise WebOAuthStateError

    async def consume_connection_test_state(
        self, *, state: str, state_cookie: str | None
    ) -> None:
        """确认这次回调属于连接测试；不属于就抛 ``WebOAuthStateError``。

        与 ``complete_connection_test`` **分成两步**是刻意的：路由必须在确认命中
        测试域之后、发起 code 交换之前，再核对一遍本地管理员 session 仍然有效。
        合成一个方法就没有插入那道检查的位置。
        """
        await self._consume_state(
            state=state, state_cookie=state_cookie, domain=OAUTH_TEST_STATE_DOMAIN
        )

    async def complete_connection_test(self, *, code: str) -> None:
        """只交换一次 code 以证明回调链路可用；成功即返回。

        **不解析身份、不轮换 session、不下发任何 cookie。** 这里测的是"飞书能不能
        把 code 换成身份"，不是"这个人能不能登录"：一个不在身份目录里的管理员完成
        测试是正常的，把它算成失败会让管理员去改身份目录而不是去查 OAuth 配置。
        """
        await self._exchange(code=code)

    async def _exchange(self, *, code: str) -> FeishuOAuthIdentity:
        """校验 code 形状并换一次身份；Provider 异常只映射到本模块的闭集。

        登录与连接测试共用这一处：两边各写一份就有两套超时上限、两套控制字符
        拒绝与两套异常映射，而"测试通过但登录失败"恰恰是最难排查的那种不一致。
        """
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
        return identity

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str | None,
        previous_session_cookie: str | None,
    ) -> IssuedWebSession:
        """消费同浏览器 state，重映射身份并原子轮换 session。"""
        await self._consume_state(
            state=state, state_cookie=state_cookie, domain=OAUTH_LOGIN_STATE_DOMAIN
        )
        identity = await self._exchange(code=code)
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
            if session.auth_source is not IdentitySource.FEISHU:
                # 本地管理员写的是**同一张** ``web_sessions`` 表、同一套 digest 域，
                # 所以"查得到 session"不蕴含"这条 session 是我签发的"。少了这一半，
                # 身份目录里只要存在一条能解析 ``local-admin`` 的条目，本地管理员的
                # cookie 就会被当成飞书身份放行。隔离必须双向——
                # ``LocalAdminAuthService.authenticate`` 那侧是这句的镜像。
                raise WebAuthenticationError
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
        validate_state_change(
            public_origin=self._public_origin,
            session_cookie=session_cookie,
            origin=origin,
            csrf_token=csrf_token,
        )

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
    "validate_state_change",
    "validate_unauthenticated_origin",
    "web_csrf_token",
    "web_origin_digest",
    "web_session_digest",
]

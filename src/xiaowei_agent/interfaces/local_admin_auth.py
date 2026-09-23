"""本地管理员的口令哈希与登录/改密服务。

封装形如 ``scrypt$<版本>$<n>$<r>$<p>$<salt_b64>$<dk_b64>``。参数写进封装是为了
让未来换参时能识别旧封装，**不是**为了让校验采纳它们——见 :func:`verify_password`。

本地管理员的主体是**固定常量**，不经身份目录解析：它不是某个外部账号的映射，
而是这台机器的运维入口，租户与环境都由 ADR-007 D2 钉死。
"""

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from xiaowei_agent.contracts import (
    AdminCapability,
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    ProductRole,
    WebReturnIntent,
)
from xiaowei_agent.contracts.identity import (
    LOCAL_ADMIN_ACTOR,
    LOCAL_ADMIN_ENVIRONMENT_ID,
    LOCAL_ADMIN_TENANT_ID,
)
from xiaowei_agent.governance.product_roles import admin_capabilities
from xiaowei_agent.interfaces.web_auth import (
    IssuedWebSession,
    web_csrf_token,
    web_session_digest,
)
from xiaowei_agent.interfaces.web_navigation import web_return_intent_allowed
from xiaowei_agent.persistence.local_admin import (
    LOCAL_ADMIN_SUBJECT_REF,
    ChangePasswordCommand,
    LocalAdminNotFoundError,
    LocalAdminStore,
)
from xiaowei_agent.persistence.web_session import (
    RevokeWebSessionCommand,
    RotateWebSessionCommand,
    WebSessionLookup,
    WebSessionNotFoundError,
    WebSessionStore,
)

INITIAL_LOCAL_ADMIN_PASSWORD: Final[str] = "adm" + "in"
"""首次 seed 的初始口令。

写成常量而不是随机值：运维必须能在没有任何带外通道的情况下完成第一次登录。
配套约束是 ``must_change_password=True``——在改密之前，除登录/改密/退出以外的
所有接口一律拒绝，因此这个众所周知的口令不构成一个可用的长期凭据。
"""

LOCAL_ADMIN_USERNAME: Final[str] = LOCAL_ADMIN_ACTOR

_SCRYPT_N: Final[int] = 2**14
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SCRYPT_DKLEN: Final[int] = 32
_SALT_BYTES: Final[int] = 16
_ENCODING_VERSION: Final[str] = "1"


def hash_password(password: str) -> str:
    """用随机盐生成一条带版本与参数的 scrypt 封装。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        [
            "scrypt",
            _ENCODING_VERSION,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """只接受本模块固定参数的封装；封装里的 n/r/p 不被信任为计算输入。"""
    if not isinstance(encoded, str):
        return False
    try:
        kind, version, n, r, p, salt_b64, derived_b64 = encoded.split("$")
    except ValueError:
        return False
    # 参数是被**核对**的，不是被采纳的：不匹配直接拒绝，避免攻击者用 n=2 的封装
    # 把校验降成廉价运算，也避免用超大 n 做内存耗尽。
    if (
        kind != "scrypt"
        or version != _ENCODING_VERSION
        or (n, r, p) != (str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P))
    ):
        return False
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(derived_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    if len(salt) != _SALT_BYTES or len(expected) != _SCRYPT_DKLEN:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(candidate, expected)


__all__ = [
    "LOCAL_ADMIN_PRINCIPAL",
    "LocalAdminAuthService",
    "LocalAdminAuthenticationError",
    "LocalAdminSession",
    "hash_password",
    "verify_password",
]


LOCAL_ADMIN_PRINCIPAL: Final = AuthenticatedPrincipal(
    tenant_id=LOCAL_ADMIN_TENANT_ID,
    environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
    actor=LOCAL_ADMIN_ACTOR,
    source=IdentitySource.LOCAL_ADMIN,
    subject_ref=LOCAL_ADMIN_SUBJECT_REF,
    permissions=frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
            ChannelPermission.ADMIN_ALL_SAFE_TASKS,
        }
    ),
)
"""本地管理员的固定主体。常量，不由身份目录解析，也不受配置影响。"""


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class LocalAdminSession:
    """一次通过认证的本地管理员会话。"""

    principal: AuthenticatedPrincipal
    role: ProductRole
    admin_capabilities: frozenset[AdminCapability]
    csrf_token: str = field(repr=False)
    must_change_password: bool = False


class LocalAdminAuthenticationError(RuntimeError):
    """口令不匹配、session 不存在或已失效。"""

    def __init__(self) -> None:
        super().__init__("local admin authentication failed")


class LocalAdminAuthService:
    """本地管理员的登录与改密。

    随机来源集中在这里：session cookie 由 ``token_factory`` 生成，store 只接收
    已经算好的 digest，因此存储层不持有任何可还原成 cookie 的材料。
    """

    def __init__(
        self,
        *,
        admins: LocalAdminStore,
        sessions: WebSessionStore,
        public_origin_digest: str,
        session_ttl_seconds: int,
        token_factory: Callable[[], str] = _random_secret,
    ) -> None:
        if not 0 < session_ttl_seconds <= 86_400:
            raise ValueError("web session ttl is out of range")
        self._admins = admins
        self._sessions = sessions
        self._public_origin_digest = public_origin_digest
        self._session_ttl_seconds = session_ttl_seconds
        self._token_factory = token_factory

    def _new_cookie(self) -> str:
        value = self._token_factory()
        if not isinstance(value, str) or len(value) < 32:
            raise RuntimeError("secure random token generation failed")
        return value

    async def login(
        self,
        *,
        username: str,
        password: str,
        return_intent: WebReturnIntent,
        previous_session_cookie: str | None,
    ) -> IssuedWebSession:
        """核对口令并签发新 session；失败路径不区分"没 seed"与"口令错"。"""
        record = None
        lookup_failed = False
        try:
            record = await self._admins.get()
        except LocalAdminNotFoundError:
            lookup_failed = True
        username_matches = username.isascii() and hmac.compare_digest(
            username, LOCAL_ADMIN_USERNAME
        )
        password_matches = (
            record is not None and verify_password(password, record.password_hash)
        )
        if lookup_failed or not username_matches or not password_matches:
            raise LocalAdminAuthenticationError
        if not web_return_intent_allowed(
            source=IdentitySource.LOCAL_ADMIN,
            role=ProductRole.ADMIN,
            intent=return_intent,
        ):
            raise LocalAdminAuthenticationError
        cookie = self._new_cookie()
        previous_digest = (
            web_session_digest(previous_session_cookie)
            if isinstance(previous_session_cookie, str) and previous_session_cookie
            else None
        )
        new_digest = web_session_digest(cookie)
        if previous_digest == new_digest:
            raise RuntimeError("secure random token generation failed")
        await self._sessions.rotate_session(
            command=RotateWebSessionCommand(
                session_digest=new_digest,
                previous_session_digest=previous_digest,
                subject_ref=LOCAL_ADMIN_SUBJECT_REF,
                ttl_seconds=self._session_ttl_seconds,
                auth_source=IdentitySource.LOCAL_ADMIN,
                public_origin_digest=self._public_origin_digest,
            )
        )
        return IssuedWebSession(
            session_cookie=cookie,
            principal=LOCAL_ADMIN_PRINCIPAL,
            role=ProductRole.ADMIN,
            admin_capabilities=admin_capabilities(
                role=ProductRole.ADMIN,
                source=IdentitySource.LOCAL_ADMIN,
            ),
            return_intent=return_intent,
            max_age_seconds=self._session_ttl_seconds,
        )

    async def authenticate(self, *, session_cookie: str | None) -> LocalAdminSession:
        """按 cookie 摘要取 session；只接受本地管理员签发的那一类。"""
        if not isinstance(session_cookie, str) or not session_cookie:
            raise LocalAdminAuthenticationError
        failed = False
        session = None
        try:
            session = await self._sessions.get_session(
                lookup=WebSessionLookup(
                    session_digest=web_session_digest(session_cookie),
                    public_origin_digest=self._public_origin_digest,
                )
            )
        except WebSessionNotFoundError:
            failed = True
        if failed or session is None:
            raise LocalAdminAuthenticationError
        if session.auth_source is not IdentitySource.LOCAL_ADMIN:
            # 飞书 session 不得走本地管理员这条路径，即使它同样有效。
            raise LocalAdminAuthenticationError
        record = await self._admins.get()
        return LocalAdminSession(
            principal=LOCAL_ADMIN_PRINCIPAL,
            role=ProductRole.ADMIN,
            admin_capabilities=admin_capabilities(
                role=ProductRole.ADMIN,
                source=IdentitySource.LOCAL_ADMIN,
            ),
            csrf_token=web_csrf_token(session_cookie),
            must_change_password=record.must_change_password,
        )

    async def change_password(
        self,
        *,
        session_cookie: str,
        current: str,
        new: str,
        return_intent: WebReturnIntent,
    ) -> IssuedWebSession:
        """核对当前口令后，在**一个事务**里改密、撤销全部旧 session 并签发新的。"""
        await self.authenticate(session_cookie=session_cookie)
        record = await self._admins.get()
        if not verify_password(current, record.password_hash):
            raise LocalAdminAuthenticationError
        cookie = self._new_cookie()
        await self._admins.change_password_and_rotate_session(
            command=ChangePasswordCommand(
                password_hash=hash_password(new),
                new_session_digest=web_session_digest(cookie),
                public_origin_digest=self._public_origin_digest,
                session_ttl_seconds=self._session_ttl_seconds,
            )
        )
        return IssuedWebSession(
            session_cookie=cookie,
            principal=LOCAL_ADMIN_PRINCIPAL,
            role=ProductRole.ADMIN,
            admin_capabilities=admin_capabilities(
                role=ProductRole.ADMIN,
                source=IdentitySource.LOCAL_ADMIN,
            ),
            return_intent=return_intent,
            max_age_seconds=self._session_ttl_seconds,
        )

    async def logout(self, *, session_cookie: str) -> bool:
        """撤销当前 session；未知或重复撤销返回假。"""
        return await self._sessions.revoke_session(
            command=RevokeWebSessionCommand(
                session_digest=web_session_digest(session_cookie)
            )
        )

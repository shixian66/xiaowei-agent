"""Web OAuth state 与浏览器 session 的 digest-only 存储契约。"""

from typing import Final, Protocol, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts import (
    AwareDatetime,
    Contract,
    IdentitySource,
    Sha256Hex,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.web_navigation import WebReturnIntent

DEFAULT_OAUTH_STATE_CAPACITY: Final[int] = 1024


class WebSessionStoreError(RuntimeError):
    """不把 cookie、state 或主体引用写入异常文本的存储错误。"""


class OAuthStateNotFoundError(WebSessionStoreError, LookupError):
    """OAuth state 不存在、已消费或已过期。"""

    def __init__(self) -> None:
        super().__init__("oauth state not found")


class OAuthStateCapacityError(WebSessionStoreError):
    """全局未完成 OAuth state 已达到固定容量。"""

    def __init__(self) -> None:
        super().__init__("oauth state capacity exhausted")


class OAuthLoginContextNotFoundError(WebSessionStoreError, LookupError):
    """登录域 state 存在但它的必需 context 缺失。"""

    def __init__(self) -> None:
        super().__init__("oauth login context missing")


class WebSessionNotFoundError(WebSessionStoreError, LookupError):
    """浏览器 session 不存在、已撤销或已过期。"""

    def __init__(self) -> None:
        super().__init__("web session not found")


class WebSessionConflictError(WebSessionStoreError):
    """随机 digest 与已有 OAuth state 或 session 冲突。"""

    def __init__(self) -> None:
        super().__init__("web session conflict")


class OAuthState(Contract):
    """只保存 OAuth state 摘要与单次消费的时效事实。"""

    state_digest: Sha256Hex
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _time_facts_are_consistent(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("oauth state expiry must be after issuance")
        if self.consumed_at is not None and not (
            self.issued_at <= self.consumed_at < self.expires_at
        ):
            raise ValueError("oauth state consumption must precede expiry")
        return self


class OAuthLoginState(OAuthState):
    """一次登录 state 与其不可分割的闭集返回意图。"""

    return_intent: WebReturnIntent


class WebSession(Contract):
    """只保存随机 cookie 摘要、主体引用、签发来源与时效事实。"""

    session_digest: Sha256Hex
    subject_ref: StrictStr
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    auth_source: IdentitySource
    public_origin_digest: Sha256Hex

    @model_validator(mode="after")
    def _time_facts_are_consistent(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("web session expiry must be after issuance")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("web session revocation cannot predate issuance")
        return self


class IssueOAuthStateCommand(Contract):
    state_digest: Sha256Hex
    ttl_seconds: StrictInt = Field(gt=0, le=600)


class IssueOAuthLoginStateCommand(IssueOAuthStateCommand):
    """登录专用签发命令；无法构造一个没有 context 的登录 state。"""

    return_intent: WebReturnIntent


class ConsumeOAuthStateCommand(Contract):
    state_digest: Sha256Hex


class ConsumeOAuthLoginStateCommand(ConsumeOAuthStateCommand):
    """登录专用消费命令；缺 context 是独立不变量错误。"""


class RotateWebSessionCommand(Contract):
    """签发一个新 session。

    ``auth_source`` 与 ``public_origin_digest`` **必填、无默认值**：给默认值等于
    允许调用方漏传而静默写入一个错误的绑定，而这两列正是"谁签发的"和"在哪个
    origin 下有效"的唯一记录。
    """

    session_digest: Sha256Hex
    subject_ref: StrictStr
    ttl_seconds: StrictInt = Field(gt=0, le=86_400)
    previous_session_digest: Sha256Hex | None = None
    auth_source: IdentitySource
    public_origin_digest: Sha256Hex

    @model_validator(mode="after")
    def _rotation_uses_a_new_digest(self) -> Self:
        if self.session_digest == self.previous_session_digest:
            raise ValueError("session rotation requires a new digest")
        return self


class WebSessionLookup(Contract):
    """按 digest 查 session，并同时核对它绑定的 public origin。

    origin digest 是**查询条件**而不是返回后再比：放在 store 层过滤，任何调用方
    都不可能忘记比对。
    """

    session_digest: Sha256Hex
    public_origin_digest: Sha256Hex


class RevokeWebSessionCommand(Contract):
    session_digest: Sha256Hex


class WebSessionStore(Protocol):
    """原子保存一次性 OAuth state 与可撤销浏览器 session。"""

    async def issue_oauth_state(
        self, *, command: IssueOAuthStateCommand
    ) -> OAuthState:
        """保存 state 摘要；随机碰撞必须拒绝。"""

    async def consume_oauth_state(
        self, *, command: ConsumeOAuthStateCommand
    ) -> OAuthState:
        """原子消费仍有效的 state；未知、过期和重放统一拒绝。"""

    async def issue_oauth_login_state(
        self, *, command: IssueOAuthLoginStateCommand
    ) -> OAuthLoginState:
        """同一提交签发登录 state 与它的闭集返回意图。"""

    async def consume_oauth_login_state(
        self, *, command: ConsumeOAuthLoginStateCommand
    ) -> OAuthLoginState:
        """同一提交消费登录 state 并读取唯一 context。"""

    async def rotate_session(
        self, *, command: RotateWebSessionCommand
    ) -> WebSession:
        """原子创建新 session，并在同一提交中撤销旧 digest。"""

    async def get_session(self, *, lookup: WebSessionLookup) -> WebSession:
        """只返回未撤销且未过期的 session。"""

    async def revoke_session(self, *, command: RevokeWebSessionCommand) -> bool:
        """撤销仍未撤销的 session；未知或重复撤销返回假。"""


def validate_oauth_state_capacity(value: int) -> int:
    """校验仅供受信 store 装配覆盖的 OAuth state 容量。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= DEFAULT_OAUTH_STATE_CAPACITY
    ):
        raise ValueError("oauth state capacity out of bounds")
    return value


__all__ = [
    "DEFAULT_OAUTH_STATE_CAPACITY",
    "ConsumeOAuthLoginStateCommand",
    "ConsumeOAuthStateCommand",
    "IssueOAuthLoginStateCommand",
    "IssueOAuthStateCommand",
    "OAuthLoginContextNotFoundError",
    "OAuthLoginState",
    "OAuthState",
    "OAuthStateCapacityError",
    "OAuthStateNotFoundError",
    "RevokeWebSessionCommand",
    "RotateWebSessionCommand",
    "WebSession",
    "WebSessionConflictError",
    "WebSessionLookup",
    "WebSessionNotFoundError",
    "WebSessionStore",
    "WebSessionStoreError",
    "validate_oauth_state_capacity",
]

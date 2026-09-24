"""Web OAuth state 与浏览器 session 的 digest-only 存储契约。"""

from typing import Final, Protocol, Self

from pydantic import Field, field_validator, model_validator

from xiaowei_agent.contracts import (
    AwareDatetime,
    Contract,
    IdentitySource,
    Sha256Hex,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.admin_audit import AuditOperationId
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


class OAuthTestContextNotFoundError(WebSessionStoreError, LookupError):
    """测试域 state 存在但它的必需 context 缺失（含：它其实是一张登录 state）。"""

    def __init__(self) -> None:
        super().__init__("oauth test context missing")


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


def _w4_operation_id(value: str) -> str:
    if not value.startswith("w4:"):
        raise ValueError("oauth test operation id must be server-derived")
    return value


class OAuthTestState(OAuthState):
    """一次 OAuth 连接测试 state、签发它的 W4a 审计 operation id 与被测飞书配置代次。

    ``config_generation`` 是签发时 Web 已加载、且等于当前文件的那一代：Web 的 OAuth
    adapter 持有的是启动期凭据，回调只能按这一代核对与记账，不能按回调时的文件重新归属。
    """

    operation_id: AuditOperationId
    config_generation: StrictInt = Field(gt=0)

    _operation_id_is_w4 = field_validator("operation_id")(_w4_operation_id)


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


class IssueOAuthTestStateCommand(IssueOAuthStateCommand):
    """测试专用签发命令；无法构造一个不绑定审计 operation 与被测代次的测试 state。"""

    operation_id: AuditOperationId
    config_generation: StrictInt = Field(gt=0)

    _operation_id_is_w4 = field_validator("operation_id")(_w4_operation_id)


class ConsumeOAuthStateCommand(Contract):
    state_digest: Sha256Hex


class ConsumeOAuthLoginStateCommand(ConsumeOAuthStateCommand):
    """登录专用消费命令；缺 context 是独立不变量错误。"""


class ConsumeOAuthTestStateCommand(ConsumeOAuthStateCommand):
    """测试专用消费命令；缺测试 context 同样是独立不变量错误，且整体回滚。"""


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

    async def issue_oauth_test_state(
        self, *, command: IssueOAuthTestStateCommand
    ) -> OAuthTestState:
        """同一提交签发测试 state 与它的审计 operation id；碰撞或 operation 复用拒绝。"""

    async def consume_oauth_test_state(
        self, *, command: ConsumeOAuthTestStateCommand
    ) -> OAuthTestState:
        """同一提交消费测试 state 并取回 operation id 与绑定代次；未知、过期和重放统一拒绝。

        state 有效但没有测试 context（包括一张登录 state）时抛
        :class:`OAuthTestContextNotFoundError` 并回滚，绝不消费。
        """

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
    "ConsumeOAuthTestStateCommand",
    "IssueOAuthLoginStateCommand",
    "IssueOAuthStateCommand",
    "IssueOAuthTestStateCommand",
    "OAuthLoginContextNotFoundError",
    "OAuthLoginState",
    "OAuthState",
    "OAuthStateCapacityError",
    "OAuthStateNotFoundError",
    "OAuthTestContextNotFoundError",
    "OAuthTestState",
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

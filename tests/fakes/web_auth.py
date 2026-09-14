"""Web 路由测试共用的认证替身。

放在 `tests/fakes/` 而不是各测试文件里：本地管理员认证是**必填**依赖，
每个 `create_app` 调用点都要给一个；各写一份会在下一次签名变更时散落失败。
"""

from collections.abc import Mapping
from typing import Any

from xiaowei_agent.contracts import LoadReceipt
from xiaowei_agent.interfaces.local_admin_auth import (
    LocalAdminAuthenticationError,
    LocalAdminSession,
)
from xiaowei_agent.persistence.provider_state import (
    ProviderStateSnapshot,
    RecordTestCommand,
)


class NoLocalAdmin:
    """永远认不出本地管理员的替身。

    用于只验证飞书 OAuth 路径的既有用例：路由层先按本地管理员解析、再回落到
    飞书，这个替身让回落一定发生，从而保持那些用例原本的语义不变。
    """

    async def authenticate(self, *, session_cookie: str | None) -> LocalAdminSession:
        raise LocalAdminAuthenticationError

    async def login(self, **_: object) -> Any:  # pragma: no cover - 本替身不参与登录
        raise AssertionError("not used")

    async def change_password(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def logout(self, *, session_cookie: str) -> bool:
        return False


class EmptyProviderState:
    """没有任何回执与测试结果的替身。

    用于不涉及配置面的既有用例：``provider_state`` 是 ``create_app`` 的必填依赖，
    每个调用点各写一份空实现会在下一次签名变更时散落失败。
    """

    def __init__(self) -> None:
        self.recorded: list[Mapping[tuple[str, str], LoadReceipt]] = []

    async def record_load(
        self, *, receipts: Mapping[tuple[str, str], LoadReceipt]
    ) -> None:
        self.recorded.append(receipts)

    async def record_test(self, *, command: RecordTestCommand) -> None:
        raise AssertionError("not used")  # pragma: no cover

    async def snapshot(self) -> ProviderStateSnapshot:
        return ProviderStateSnapshot(receipts={}, tests={})


__all__ = ["EmptyProviderState", "NoLocalAdmin"]

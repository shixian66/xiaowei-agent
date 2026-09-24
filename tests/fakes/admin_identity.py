"""不涉及 W3-lite 身份管理路由的 Web 测试共用替身。"""


class UnusedAdminIdentity:
    """任何误入身份管理服务的调用都立即让原测试失败。"""

    def __getattr__(self, name: str) -> object:  # pragma: no cover - 误调用即失败
        raise AssertionError(name)


__all__ = ["UnusedAdminIdentity"]

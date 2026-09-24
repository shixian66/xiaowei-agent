"""不涉及配置写入的 Web 测试共用的配置服务替身。"""

from typing import Any


class AbsentIntegrationConfig:
    """两个配置域都"尚未配置"；任何写入或探针调用都立即让原测试失败。"""

    async def read_ai(self) -> None:
        return None

    async def read_feishu(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - 误调用即失败
        raise AssertionError(name)


__all__ = ["AbsentIntegrationConfig"]

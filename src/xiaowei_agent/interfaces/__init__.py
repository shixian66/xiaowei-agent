"""HTTP、OAuth、CLI、Worker 与迁移入口。"""

from typing import Final

FEISHU_PROVIDER_ORIGIN: Final[str] = "https://open.feishu.cn"
GEMINI_PROVIDER_ORIGIN: Final[str] = "https://generativelanguage.googleapis.com"

__all__ = ["FEISHU_PROVIDER_ORIGIN", "GEMINI_PROVIDER_ORIGIN"]

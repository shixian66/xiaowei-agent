"""小维 Agent 2.0：策略治理型运维工作流 Agent。

版本号的唯一真源是 ``pyproject.toml`` 的 ``[project].version``；
本模块只通过包元数据读取，不重复声明。
"""

from importlib.metadata import version
from typing import Final

__version__: Final[str] = version("xiaowei-agent")

__all__ = ["__version__"]

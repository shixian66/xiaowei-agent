"""模块依赖必须单向。

分层自下而上是：标准库 → redaction.py（无对内依赖的叶子）→ contracts/ → 其余业务
包。依赖方向一旦反转，"契约不随框架变化"就失效。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

_ALLOWED_INTERNAL = {
    # contracts 允许且仅允许依赖 redaction（无对内依赖的叶子）与标准库；
    # log / config / trace 一律不可。
    "contracts": {"xiaowei_agent.contracts", "xiaowei_agent.redaction"},
    "capabilities": {"xiaowei_agent.contracts", "xiaowei_agent.capabilities"},
    "planning": {"xiaowei_agent.contracts", "xiaowei_agent.planning"},
    "governance": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.planning",
        "xiaowei_agent.governance",
    },
    "tools": {"xiaowei_agent.contracts", "xiaowei_agent.planning", "xiaowei_agent.tools"},
    "persistence": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.planning",
        "xiaowei_agent.persistence",
    },
    "runners": {
        "xiaowei_agent.contracts",
        "xiaowei_agent.persistence",
        "xiaowei_agent.runners",
    },
    "observability": {"xiaowei_agent.contracts", "xiaowei_agent.observability"},
}


def _internal_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            found.add(".".join((module or "").split(".")[:2]))
    return found


@pytest.mark.parametrize("package", sorted(_ALLOWED_INTERNAL))
def test_package_only_imports_allowed_internal_modules(package: str) -> None:
    allowed = _ALLOWED_INTERNAL[package]
    offenders: list[tuple[str, str]] = []
    for path in (_SRC / package).rglob("*.py"):
        for module in _internal_imports(path):
            if module not in allowed:
                offenders.append((str(path.relative_to(_SRC)), module))
    assert not offenders, f"{package} 出现非法内部依赖: {offenders}"


def test_contracts_never_depend_on_implementations() -> None:
    """契约反向依赖实现，会让替换实现牵动契约。"""
    banned = {
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
        "xiaowei_agent.persistence",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.planning",
        "xiaowei_agent.governance",
        "xiaowei_agent.log",
        "xiaowei_agent.config",
        "xiaowei_agent.trace",
    }
    for path in (_SRC / "contracts").rglob("*.py"):
        assert not (_internal_imports(path) & banned), path


def test_redaction_is_a_leaf() -> None:
    assert not _internal_imports(_SRC / "redaction.py")

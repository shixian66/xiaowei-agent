"""``evidence/`` 必须是纯构造器。

这条测试是 ``runners → evidence`` 这条依赖边的**对价**。Runner 的依赖面已经是全
项目最宽的一条，只有在 evidence 确实无法承载策略、状态或动态计划时，那条边才是
安全的。撤掉这条测试，那条依赖边就失去了正当性。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

_ALLOWED_INTERNAL = {
    "xiaowei_agent.contracts",
    "xiaowei_agent.evidence",
    "xiaowei_agent.redaction",
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


def _evidence_modules() -> list[Path]:
    return sorted((_SRC / "evidence").rglob("*.py"))


def test_evidence_package_exists_and_is_scanned() -> None:
    """空扫描会让下面每一条断言都恒真。"""
    assert _evidence_modules()


def test_evidence_package_has_no_async_functions() -> None:
    """无 async 即无 I/O 等待点：证据构造不可能变成一次外部调用。"""
    for path in _evidence_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
        assert not offenders, path


def test_evidence_package_only_depends_on_contracts() -> None:
    """不得依赖 persistence / governance / tools / planning / runners。

    一旦它能读写存储或触达策略，它就不再是"把已发生的事实包起来"，而成了第二个
    可以做决定的地方。
    """
    for path in _evidence_modules():
        assert _internal_imports(path) <= _ALLOWED_INTERNAL, path


def test_evidence_package_imports_no_third_party_client() -> None:
    banned = {"sqlglot", "pymysql", "psycopg", "sqlalchemy", "httpx", "requests"}
    for path in _evidence_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                module = node.names[0].name
            assert (module or "").split(".")[0] not in banned, path

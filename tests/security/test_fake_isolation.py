"""fake 随包发布（DEVELOPMENT_PLAN §7 M2 明文要求），因此必须可被审计地隔离。

代价是"fake 可能被接进真实路径"，用三条断言抵消：每个 fake 模块自我标识、不从包
入口导出、且没有任何生产模块导入它。
"""

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_FAKE_MODULES = ("tools.fake", "persistence.fake", "runners.fake")


@pytest.mark.parametrize("name", _FAKE_MODULES)
def test_every_fake_module_declares_is_fake(name: str) -> None:
    module = importlib.import_module(f"xiaowei_agent.{name}")
    assert getattr(module, "IS_FAKE", False) is True


def _imports_a_fake(path: Path) -> list[str]:
    """扫 AST 的 import，而不是原始文本。

    文本扫描会被 docstring 里"不从本入口导出 fake"这句话本身触发，断言的就不再
    是代码行为。
    """
    hits: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").endswith(".fake"):
            hits.append(module or "")
        if isinstance(node, ast.ImportFrom) and any(a.name == "fake" for a in node.names):
            hits.append(f"{node.module}.fake")
    return hits


@pytest.mark.parametrize("name", _FAKE_MODULES)
def test_fake_is_not_exported_from_its_package_init(name: str) -> None:
    package, _, _ = name.partition(".")
    init = _SRC / package / "__init__.py"
    assert not _imports_a_fake(init), f"{package}/__init__.py 不得导出 fake"


def test_the_detector_catches_both_import_spellings(tmp_path: Path) -> None:
    """检测器自身必须先被证明有效。"""
    a = tmp_path / "a.py"
    a.write_text("from xiaowei_agent.tools import fake" + chr(10), encoding="utf-8")
    assert _imports_a_fake(a)

    b = tmp_path / "b.py"
    b.write_text(
        "from xiaowei_agent.tools.fake import RecordingToolAdapter" + chr(10),
        encoding="utf-8",
    )
    assert _imports_a_fake(b)

    c = tmp_path / "c.py"
    c.write_text(
        '"""docstring 提到 fake 但不 import。"""' + chr(10)
        + "from xiaowei_agent import contracts" + chr(10),
        encoding="utf-8",
    )
    assert not _imports_a_fake(c)


def test_no_production_module_imports_a_fake() -> None:
    fake_paths = {_SRC / f"{name.replace('.', '/')}.py" for name in _FAKE_MODULES}
    offenders = [
        (str(path.relative_to(_SRC)), hit)
        for path in _SRC.rglob("*.py")
        if path not in fake_paths
        for hit in _imports_a_fake(path)
    ]
    assert not offenders, f"生产模块不得导入 fake: {offenders}"

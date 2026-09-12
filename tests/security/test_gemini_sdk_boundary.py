"""官方模型 SDK 只能存在于单一 interface seam。"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"
_SEAM = _SRC / "interfaces" / "gemini_model.py"


def _google_genai_references(path: Path) -> set[str]:
    references: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            references.update(
                alias.name for alias in node.names if alias.name.startswith("google")
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("google"):
            references.add(node.module or "")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "google.genai" in node.value:
                references.add(node.value)
    return references


def test_google_genai_imports_and_dynamic_strings_exist_only_in_the_sdk_seam() -> None:
    found = {
        path.relative_to(_SRC).as_posix(): _google_genai_references(path)
        for path in _SRC.rglob("*.py")
        if _google_genai_references(path)
    }
    assert set(found) == {"interfaces/gemini_model.py"}
    assert found[_SEAM.relative_to(_SRC).as_posix()] == {
        "google",
        "google.genai",
    }


def test_core_imports_do_not_load_the_model_sdk() -> None:
    code = """
import sys
import xiaowei_agent.contracts
import xiaowei_agent.config
import xiaowei_agent.interfaces.local_stack
import xiaowei_agent.application.runtime
import xiaowei_agent.application.worker
raise SystemExit(1 if 'google.genai' in sys.modules else 0)
"""
    result = subprocess.run(  # noqa: S603 -- 当前解释器固定执行内联审计脚本
        [sys.executable, "-c", code],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_sdk_seam_contains_no_retry_or_timeout_or_caller_endpoint_surface() -> None:
    tree = ast.parse(_SEAM.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "timeout" not in names
    assert "sleep" not in names
    assert "retry" not in names
    assert "base_url" not in {
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for arg in (*node.args.args, *node.args.kwonlyargs)
    }
    assert "generate_content" in attributes

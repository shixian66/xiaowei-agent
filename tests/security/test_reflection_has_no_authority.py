"""Reflection 只消费已生成的结构化证据，不拥有任何执行权。

它可以说"证据不够"，不能说"再去取一次"。两者的差别在契约层就被固定下来：
``AnswerabilityVerdict`` 的字段集里没有步骤、工具、目标、权限或 SQL。
"""

import ast
from pathlib import Path

import pytest

from xiaowei_agent.reflection import answerability

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

_BANNED_IMPORTS = {
    "xiaowei_agent.tools",
    "xiaowei_agent.governance",
    "xiaowei_agent.persistence",
    "xiaowei_agent.runners",
    "xiaowei_agent.planning",
    "xiaowei_agent.capabilities",
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


def _reflection_modules() -> list[Path]:
    return sorted((_SRC / "reflection").rglob("*.py"))


def test_reflection_package_is_scanned() -> None:
    assert _reflection_modules()


def test_reflection_cannot_reach_tools_storage_or_governance() -> None:
    """契约层面不存在让 Reflection 修改计划或写 TaskStore 的入口。"""
    for path in _reflection_modules():
        assert not (_internal_imports(path) & _BANNED_IMPORTS), path


def test_reflection_has_no_async_functions() -> None:
    """无 async 即无 I/O 等待点：它不可能变成一次取数。"""
    for path in _reflection_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)], path


def test_assess_only_takes_evidence() -> None:
    """签名里没有 plan / target / gateway / store——越权在入参上就不可表达。"""
    import inspect

    parameters = set(inspect.signature(answerability.assess).parameters)
    assert parameters == {"evidences"}


def test_rendering_cannot_reach_tools_storage_or_governance() -> None:
    for path in sorted((_SRC / "rendering").rglob("*.py")):
        assert not (_internal_imports(path) & _BANNED_IMPORTS), path


def test_reflection_never_constructs_a_plan_or_a_tool_call() -> None:
    banned = {"ExecutionPlan", "PlanStep", "ToolCall", "AdmissionCertificate"}
    for path in [*_reflection_modules(), *sorted((_SRC / "rendering").rglob("*.py"))]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not (called & banned), path

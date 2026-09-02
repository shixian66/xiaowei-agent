"""分类字段只能由唯一构造器产生。

禁止的不是"赋值分类字段"——M3 的 PlanCompiler 本就必须构造带分类的步骤。正确
口径是：除 ``capabilities/effect.py`` 外，任何模块都不得**直接构造** PlanStep；
PlanCompiler 调用 ``build_plan_step()`` 即合规。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_ALLOWED = {
    _SRC / "capabilities" / "effect.py",
    _SRC / "contracts" / "plan.py",  # 定义处
}


def _constructs_plan_step(path: Path) -> bool:
    """检测对 PlanStep 的构造，覆盖三种写法。

    只匹配裸名 ``PlanStep(...)`` 会漏掉 ``contracts.PlanStep(...)``、
    ``plan.PlanStep(...)`` 与 ``import PlanStep as PS`` 后的 ``PS(...)``，
    使这条禁令形同虚设。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "PlanStep"
    }
    aliases.add("PlanStep")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            return True
        if isinstance(func, ast.Attribute) and func.attr == "PlanStep":
            return True
    return False


def test_the_detector_catches_all_three_spellings(tmp_path: Path) -> None:
    """检测器自身必须先被证明有效，否则这条禁令可能一直是空的。"""
    for source in (
        "from xiaowei_agent.contracts import PlanStep\nPlanStep(step_id='s')\n",
        "from xiaowei_agent.contracts import PlanStep as PS\nPS(step_id='s')\n",
        "from xiaowei_agent import contracts\ncontracts.PlanStep(step_id='s')\n",
    ):
        probe = tmp_path / "probe.py"
        probe.write_text(source, encoding="utf-8")
        assert _constructs_plan_step(probe), source


def test_the_detector_does_not_flag_unrelated_calls(tmp_path: Path) -> None:
    probe = tmp_path / "clean.py"
    probe.write_text("x = dict(step_id='s')\n", encoding="utf-8")
    assert not _constructs_plan_step(probe)


def test_only_effect_module_constructs_plan_step() -> None:
    offenders = [
        path.relative_to(_SRC)
        for path in _SRC.rglob("*.py")
        if path not in _ALLOWED and _constructs_plan_step(path)
    ]
    assert not offenders, f"以下模块不得直接构造 PlanStep，请改用 build_plan_step(): {offenders}"

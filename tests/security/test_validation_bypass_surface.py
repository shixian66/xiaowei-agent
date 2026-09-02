"""未校验通道的收口。

``Contract.model_copy`` 的覆盖只挡住**绑定调用**。还有两条同类通道：
``BaseModel.model_copy(obj, update=...)`` 这种未绑定调用会跳过子类覆盖（实证可
写入违反 ``Field(gt=0)`` 的值），而 ``_copy_within_validation`` 是我们自己为
after-validator 保留的未校验入口。两者都必须由源码扫描限定调用点，否则上面封死
的绕过只是换了个名字继续存在。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_BASE = _SRC / "contracts" / "base.py"
# 仅有的两个 after-validator 会在校验途中复制自身。新增调用点必须同时更新此处，
# 并说明为何该 validator 是幂等的。
_ALLOWED_ESCAPE_HATCH = {
    _BASE,
    _SRC / "contracts" / "target.py",
    _SRC / "contracts" / "trace_events.py",
}


def _referenced_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_escape_hatch_is_confined_to_its_declared_call_sites() -> None:
    offenders = [
        path.relative_to(_SRC)
        for path in _SRC.rglob("*.py")
        if path not in _ALLOWED_ESCAPE_HATCH
        and "_copy_within_validation" in _referenced_names(path)
    ]
    assert not offenders, f"_copy_within_validation 只允许在 after-validator 中使用: {offenders}"


def _unbound_bypass_calls(path: Path) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"model_copy", "model_construct"}:
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in {"BaseModel", "Contract"}:
            hits.append(func.attr)
    return hits


def test_no_unbound_basemodel_bypass_outside_base() -> None:
    """BaseModel.model_copy(obj, ...) 是未绑定调用，会跳过 Contract 的覆盖。"""
    offenders = [
        (str(path.relative_to(_SRC)), hit)
        for path in _SRC.rglob("*.py")
        if path != _BASE
        for hit in _unbound_bypass_calls(path)
    ]
    assert not offenders, f"禁止未绑定的校验绕过调用: {offenders}"


def test_the_detector_catches_an_unbound_bypass(tmp_path: Path) -> None:
    """检测器自身必须先被证明有效，否则这条禁令可能一直是空的。"""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from pydantic import BaseModel\n"
        "def f(x):\n    return BaseModel.model_copy(x, update={'n': -1})\n",
        encoding="utf-8",
    )
    assert _unbound_bypass_calls(probe) == ["model_copy"]


def test_the_detector_ignores_ordinary_bound_calls(tmp_path: Path) -> None:
    probe = tmp_path / "clean.py"
    probe.write_text("def f(o):\n    return o.model_copy(update={'n': 1})\n", encoding="utf-8")
    assert _unbound_bypass_calls(probe) == []


def test_escape_hatch_detector_catches_a_stray_call(tmp_path: Path) -> None:
    probe = tmp_path / "stray.py"
    probe.write_text("def f(o):\n    return o._copy_within_validation(a=1)\n", encoding="utf-8")
    assert "_copy_within_validation" in _referenced_names(probe)

"""未校验通道的收口。

``Contract.model_copy`` 的覆盖只挡住**绑定调用**。
``BaseModel.model_copy(obj, update=...)`` 这种未绑定调用会跳过子类覆盖（实证可
写入违反 ``Field(gt=0)`` 的值），因此必须由源码扫描禁止。

计划原本要为 after-validator 保留一个未校验的 ``_copy_within_validation``。实现
时发现 Pydantic v2 的 ``model_validator(mode="after")`` 经 ``__init__`` 构造时会
**丢弃**返回的非 ``self`` 对象（只发一条警告），那条路本就不成立；规范化改用
**字段级** ``AfterValidator`` 后该逃生口再无调用者，已从基类删除。本文件因此断言
它**始终不存在**——保留一个没人用的未校验通道，等于把封死的绕过换个名字留着。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_BASE = _SRC / "contracts" / "base.py"


def _referenced_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_no_unvalidated_copy_escape_hatch_exists_anywhere() -> None:
    """未校验复制的逃生口必须完全不存在，而不是被白名单限制。

    需要在校验期改写取值时，用**字段级** AfterValidator——model 级 after-validator
    返回非 self 的对象在 __init__ 路径上会被丢弃，规范化会静默失效。
    """
    offenders = [
        path.relative_to(_SRC)
        for path in _SRC.rglob("*.py")
        if "_copy_within_validation" in _referenced_names(path)
    ]
    assert not offenders, f"不得引入未校验复制通道: {offenders}"


def test_model_level_after_validators_always_return_self() -> None:
    """model_validator(mode="after") 内不得出现返回其他对象的写法。

    Pydantic 只发一条警告就丢弃返回值，规范化静默失效——这类缺陷无法由普通
    断言发现，只能在源码层禁止。
    """
    import ast

    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorated = any(
                (isinstance(d, ast.Call) and getattr(d.func, "id", "") == "model_validator")
                or getattr(d, "id", "") == "model_validator"
                for d in node.decorator_list
            )
            if not decorated:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and not (
                    isinstance(inner.value, ast.Name) and inner.value.id == "self"
                ):
                    offenders.append(f"{path.relative_to(_SRC)}:{node.name}")
    assert not offenders, f"model 级 after-validator 只能 return self: {offenders}"


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
    """检测器自身必须先被证明有效。"""
    probe = tmp_path / "stray.py"
    probe.write_text("def f(o):\n    return o._copy_within_validation(a=1)\n", encoding="utf-8")
    assert "_copy_within_validation" in _referenced_names(probe)

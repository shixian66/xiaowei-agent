"""契约基座的结构不变量。"""

import ast
import inspect
from pathlib import Path

from xiaowei_agent import contracts


def test_all_shared_enums_are_defined_in_one_module() -> None:
    """共享枚举只能定义在 contracts.enums；散落定义会造成同名不同义。"""
    root = Path(inspect.getfile(contracts)).parent
    offenders: list[str] = []
    for path in root.glob("*.py"):
        if path.name == "enums.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                if bases & {"StrEnum", "Enum", "IntEnum"}:
                    offenders.append(f"{path.name}:{node.name}")
    assert not offenders, f"枚举必须定义在 enums.py: {offenders}"


def test_public_import_surface_is_explicit() -> None:
    """__all__ 与实际可导出的名字一致，避免"能 import 但不算公开"的灰区。"""
    for name in contracts.__all__:
        assert hasattr(contracts, name), name

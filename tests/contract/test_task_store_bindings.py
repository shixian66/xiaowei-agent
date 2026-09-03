"""共享套件的绑定完整性。

抽出共享套件解决了"两个实现各自跑偏"，但换来三个**新的**静默失效路径。它们的共同
形态是"看起来写了测试，实际一次都没跑"，而全部现有用例照样全绿：

1. **用例漏进分组**：函数写在套件里，但没加进任何 ``*_CASES`` 元组，于是没有任何
   绑定会挂它。
2. **绑定漏挂分组**：新增了一个实现的绑定目录，但只绑了三组中的两组。
3. **元测试不知道新绑定**：加了绑定模块却没登记进下面的表，于是它完全不被本文件
   检查——护栏本身被绕过。

三条各由一个断言承重。第 3 条用 AST 扫描而不是靠人记得登记，理由与
``test_module_layering.py::test_every_existing_package_is_registered`` 相同：覆盖
完整性必须由机制保证，否则漏登记与"检查通过"无法区分。
"""

import ast
import importlib
from pathlib import Path
from types import ModuleType

from tests.suites import task_store as suite

_TESTS_ROOT = Path(__file__).resolve().parents[1]

# 绑定登记表：模块路径 → 它绑定的分组与实现种类。
#
# T5 已为每一组接上 ``postgres`` 绑定，因此
# ``test_every_group_is_bound_by_the_same_kinds`` 与
# ``test_binding_exposes_exactly_its_group`` 从平凡真变成**真正在比较两个实现**。
# 两条在 T5 都复查过确实会因缺绑定而转红。
_BINDINGS: dict[str, tuple[str, str]] = {
    "tests.contract.test_task_store_contract": ("contract", "memory"),
    "tests.security.test_lease_fencing": ("lease_fencing", "memory"),
    "tests.security.test_terminal_protection": ("terminal_protection", "memory"),
    "tests.integration.test_task_store_contract_postgres": ("contract", "postgres"),
    "tests.integration.test_lease_fencing_postgres": ("lease_fencing", "postgres"),
    "tests.integration.test_terminal_protection_postgres": (
        "terminal_protection",
        "postgres",
    ),
}


def _suite_case_names() -> set[str]:
    """套件模块里定义的全部用例函数名。"""
    return {
        name
        for name, value in vars(suite).items()
        if name.startswith("test_") and callable(value)
    }


def _grouped_case_names() -> list[str]:
    return [case.__name__ for group in suite.ALL_GROUPS.values() for case in group]


def _modules_calling_bind() -> set[str]:
    """AST 扫描：``tests/`` 下所有调用了 ``bind(...)`` 的模块，返回 import 路径。"""
    found: set[str] = set()
    for path in _TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "bind":
                    relative = path.relative_to(_TESTS_ROOT.parent).with_suffix("")
                    found.add(".".join(relative.parts))
    return found


def test_every_suite_case_belongs_to_exactly_one_group() -> None:
    """漏进分组的用例在**所有**绑定里都收不到，且不会有任何测试失败。"""
    grouped = _grouped_case_names()
    assert len(grouped) == len(set(grouped)), "同一用例被登记进多个分组"
    assert set(grouped) == _suite_case_names()


def test_every_module_that_binds_is_registered() -> None:
    """漏登记的绑定完全不被本文件检查——护栏被绕过，而一切照常全绿。"""
    assert _modules_calling_bind() == set(_BINDINGS)


def test_every_group_has_at_least_one_binding() -> None:
    bound_groups = {group for group, _ in _BINDINGS.values()}
    assert bound_groups == set(suite.ALL_GROUPS)


def test_every_group_is_bound_by_the_same_kinds() -> None:
    """每一组必须被**同一批实现**绑定。

    否则会出现"两组绑了 PostgreSQL、第三组忘了"这种局部覆盖：判定标准 1 说的是
    *同一套*用例在两个实现上逐条通过，少一组就不成立，而没有任何用例会失败。
    """
    kinds_by_group: dict[str, set[str]] = {group: set() for group in suite.ALL_GROUPS}
    for group, kind in _BINDINGS.values():
        kinds_by_group[group].add(kind)
    assert len(set(map(frozenset, kinds_by_group.values()))) == 1, kinds_by_group


def _load(module_path: str) -> ModuleType:
    return importlib.import_module(module_path)


def test_binding_exposes_exactly_its_group() -> None:
    """每个绑定模块暴露的套件用例，恰为它登记的那一组。

    比较范围限定在套件用例名内，因此绑定模块自己的本地用例（如
    ``test_terminal_protection.py`` 里那几条与存储无关的契约断言）不受影响。

    由此推出的正是判定标准 1 要的东西：同一组的任意两个绑定都等于该组，因而彼此
    相等。写成"逐个绑定对齐分组"而不是"两两比较"，是为了让失败信息直接指出**哪个
    绑定少了哪条**，而不是只说"两边不一样"。
    """
    universe = _suite_case_names()
    for module_path, (group, _) in _BINDINGS.items():
        module = _load(module_path)
        exposed = {name for name in vars(module) if name in universe}
        expected = {case.__name__ for case in suite.ALL_GROUPS[group]}
        assert exposed == expected, f"{module_path} 暴露的套件用例与分组 {group} 不符"

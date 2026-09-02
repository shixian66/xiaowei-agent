"""每个契约字段的字符串类型必须显式声明它属于哪一档。

**这条测试针对的是根因,不是某几个字段。** 四轮复审都没抓到 ``approval_ref:
str | None``,因为它在类型上"看起来对";真正的成因是:必填字段写 ``StrictStr``、
可选字段要写 ``StrictStr | None`` 这个联合,写的时候把别名丢掉不会有任何反馈。
逐个补字段治不了这个——下一个可选字段会以同样方式漏掉。

因此这里用 AST 扫描全部 ``Contract`` 子类,禁止裸 ``str`` 出现在字段标注里。
作者必须在三档里挑一个:``StrictStr``(标识符/引用)、``NonEmptyText``(本系统
生成的文本)、``FreeText``(外部不可信文本逐字捕获)。挑哪个是设计决定,而"没挑"
不再是一个可能的状态。

用 AST 而非文本扫描:文档字符串里出现 ``str`` 不该触发断言(参见
``test_runner_never_touches_a_gateway_or_adapter`` 的同类教训)。
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.security

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

# 三档别名 + 已有的窄类型。裸 ``str`` 不在其中,这正是本测试的要点。
APPROVED = frozenset({"StrictStr", "NonEmptyText", "FreeText", "TraceId", "Sha256Hex"})


def _contract_fields(source: str) -> list[tuple[str, str, set[str]]]:
    """返回 (类名, 字段名, 标注里出现的名字集合)。

    只收 ``Contract`` 子类;子类关系在同一文件内传递求闭包。
    """
    tree = ast.parse(source)
    kinds = {"Contract"}
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for _ in range(len(classes) + 1):  # 传递闭包,继承链最长不超过类数
        for cls in classes:
            names = {b.id for b in cls.bases if isinstance(b, ast.Name)}
            if names & kinds:
                kinds.add(cls.name)

    out: list[tuple[str, str, set[str]]] = []
    for cls in classes:
        if not ({b.id for b in cls.bases if isinstance(b, ast.Name)} & kinds):
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                used = {n.id for n in ast.walk(stmt.annotation) if isinstance(n, ast.Name)}
                out.append((cls.name, stmt.target.id, used))
    return out


def test_no_contract_field_is_annotated_with_bare_str() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for cls, field, used in _contract_fields(path.read_text(encoding="utf-8")):
            if "str" in used:
                offenders.append(f"{path.relative_to(SRC)}::{cls}.{field}")
    assert offenders == [], (
        "以下字段用了裸 str,必须显式选择 StrictStr / NonEmptyText / FreeText:\n"
        + "\n".join(offenders)
    )


def test_every_contract_string_field_uses_an_approved_alias() -> None:
    """反面覆盖:不只是"没有裸 str",还必须确实用了三档之一。

    单测"没有裸 str"会被 ``Any``、``object`` 之类绕过。
    """
    seen: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        for _cls, _field, used in _contract_fields(path.read_text(encoding="utf-8")):
            seen |= used & APPROVED
    # 三档都必须真的有人在用,否则说明分档只是摆设
    assert {"StrictStr", "NonEmptyText", "FreeText"} <= seen


@pytest.mark.parametrize(
    ("annotation", "flagged"),
    [
        ("str", True),
        ("str | None", True),
        ("tuple[str, ...]", True),
        ("StrictStr", False),
        ("StrictStr | None", False),
        ("tuple[NonEmptyText, ...]", False),
        ("FreeText", False),
    ],
)
def test_detector_itself_catches_bare_str(annotation: str, flagged: bool) -> None:
    """检测器自测。

    一个恒为空的 offenders 列表同样能让上面两条断言通过;必须先证明检测器真的会
    对裸 str 报警,前面的绿色才有意义。
    """
    source = f"class Probe(Contract):\n    field: {annotation}\n"
    fields = _contract_fields(source)
    assert len(fields) == 1
    assert ("str" in fields[0][2]) is flagged


def test_detector_ignores_str_in_docstrings_and_non_contract_classes() -> None:
    """两条反例:文本扫描会误报的位置,AST 扫描必须放行。"""
    with_docstring = (
        'class Probe(Contract):\n'
        '    """这里提到 str 只是散文,不是标注。"""\n'
        "    field: StrictStr\n"
    )
    assert _contract_fields(with_docstring) == [("Probe", "field", {"StrictStr"})]

    not_a_contract = "class Helper:\n    field: str\n"
    assert _contract_fields(not_a_contract) == []


def test_every_module_under_tests_security_declares_the_marker() -> None:
    """``tests/security/`` 下漏掉 ``pytestmark`` 的模块不会进 ``-m security`` gate。

    这是无声失败:文件放在安全目录里、``python -m pytest -q`` 也跑,唯独独立的
    安全 CI gate 不覆盖它——本轮新增的两个文件就是这样漏的(``-m security``
    仍停在 390,只有 deselected 数变了)。
    """
    root = pathlib.Path(__file__).resolve().parent
    missing = [
        p.name
        for p in sorted(root.glob("test_*.py"))
        if "pytestmark = pytest.mark.security" not in p.read_text(encoding="utf-8")
    ]
    assert missing == [], f"以下安全测试模块未声明 security marker: {missing}"

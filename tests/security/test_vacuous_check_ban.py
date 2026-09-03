"""空洞检查禁令：CAS 的两侧必须**异源**。

M3 首轮深档验收的阻断项就是这个形态——"当前值"从存储读出来再和存储比，检查恒真。
M4 把同一个陷阱搬到了 SQL 里：``SELECT ... FOR UPDATE`` 拿到的行既是当前值，又
"看起来像"期望版本，于是 ``WHERE version = <刚读到的值>`` 永远成立。

**这条禁令为什么需要一个机械检查，而不是靠反证。**

``PostgresTaskStore.transition`` 有两道独立的版本闸：``classify_transition``（Python
侧，两个实现共用）和 ``UPDATE ... WHERE version``（SQL 侧）。在 ``FOR UPDATE`` 之下
第一道先命中，因此把 SQL 那道改成"读到的值"**不会让任何行为用例转红**——即使有真实
数据库也不会。它是纵深防御，而纵深防御按定义没有单独的行为反证。

没有本文件，第二道闸就可以被悄悄改成恒真，而所有测试照常全绿；等到某天有人重构掉
第一道闸，两道闸会一起消失。因此这里用 AST 直接检查语句怎么写的。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SOURCE = Path(__file__).resolve().parents[2] / "src/xiaowei_agent/persistence/postgres.py"


def _transition_body() -> ast.AsyncFunctionDef:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "transition":
            return node
    raise AssertionError("PostgresTaskStore.transition 不见了")


def _attribute_chains(node: ast.AST) -> set[str]:
    chains: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            chains.add(f"{child.value.id}.{child.attr}")
    return chains


def test_the_cas_predicate_uses_the_callers_expected_version() -> None:
    chains = _attribute_chains(_transition_body())
    assert "command.expected_version" in chains, (
        "CAS 必须比对**调用方传入的**版本；用事务内读到的值会让检查恒真"
    )


def test_the_transition_never_reads_a_version_off_the_current_row() -> None:
    """``current.version`` / ``updated.version`` 不得出现在 ``transition`` 里的任何
    比较中。

    ``updated.version`` 出现在 ``SET version = ...`` 是正当的——那是**写入**新值，
    不是比较。因此这里只禁 ``current.version``：它是唯一"从存储读出来"的版本值。
    """
    chains = _attribute_chains(_transition_body())
    assert "current.version" not in chains


def test_the_ban_would_notice_the_forbidden_shape() -> None:
    """反例：确认上面两条真的有分辨力，而不是恰好都为真。"""
    forbidden = ast.parse(
        "async def transition(self):\n"
        "    current = read()\n"
        "    return update().where(TASKS.c.version == current.version)\n"
    )
    (function,) = forbidden.body
    assert isinstance(function, ast.AsyncFunctionDef)
    chains = _attribute_chains(function)
    assert "current.version" in chains
    assert "command.expected_version" not in chains

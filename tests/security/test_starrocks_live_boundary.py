"""M6b 真实 StarRocks 路径的零网络与代码所有权边界。"""

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Never

import pytest

from xiaowei_agent.config import _STARROCKS_GATEWAY_TIMEOUT_SECONDS
from xiaowei_agent.governance.profiles import MAX_READONLY_TIMEOUT_SECONDS
from xiaowei_agent.interfaces.local_stack import StarRocksLiveAssembly
from xiaowei_agent.runners.deterministic import DEFAULT_TIMEOUT_SECONDS
from xiaowei_agent.tools.starrocks import (
    _M6B_GATEWAY_TIMEOUT_SECONDS,
    PhysicalIdentityProbe,
)

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"


def _unreachable_factory(password: str) -> Never:
    del password
    raise AssertionError("live assembly validation reached the connection factory")


def test_m6b_timeout_bounds_match_the_call_and_policy_budget() -> None:
    assert (
        _M6B_GATEWAY_TIMEOUT_SECONDS
        == _STARROCKS_GATEWAY_TIMEOUT_SECONDS
        == DEFAULT_TIMEOUT_SECONDS
        == MAX_READONLY_TIMEOUT_SECONDS
        == 30
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SHOW FRONTENDS",
        "SELECT `cluster_identity` FROM `meta`.`identity`; SELECT 1",
        "SELECT `cluster_identity` FROM `meta`.`identity`",
        "SELECT CONCAT(`cluster_identity`, 'x') FROM `meta`.`identity` LIMIT 1",
        "SELECT `other_identity` FROM `meta`.`identity` LIMIT 1",
        "SELECT `cluster_identity` FROM `identity` LIMIT 1",
        (
            "SELECT `cluster_identity` FROM `meta`.`identity` "
            "WHERE SLEEP(1) = 0 LIMIT 1"
        ),
        (
            "SELECT `cluster_identity` FROM `meta`.`identity` "
            "ORDER BY `cluster_identity` LIMIT 1"
        ),
        (
            "SELECT `cluster_identity` FROM `meta`.`identity` "
            "JOIN `meta`.`other` ON 1 = 1 LIMIT 1"
        ),
    ],
)
def test_physical_identity_probe_rejects_expanded_sql_surface(statement: str) -> None:
    with pytest.raises(ValueError):
        PhysicalIdentityProbe(statement=statement, result_column="cluster_identity")


def test_live_assembly_requires_explicit_rows_approval_and_paired_driver() -> None:
    probe = PhysicalIdentityProbe(
        statement="SELECT `cluster_identity` FROM `meta`.`identity` LIMIT 1",
        result_column="cluster_identity",
    )
    with pytest.raises(ValueError):
        StarRocksLiveAssembly(identity_probe=probe)
    with pytest.raises(ValueError):
        StarRocksLiveAssembly(
            identity_probe=probe,
            connection_factory=_unreachable_factory,
            unredacted_rows_approved=True,
        )


def test_importing_composition_does_not_import_pymysql_or_open_a_connection() -> None:
    code = (
        "import sys; import xiaowei_agent.interfaces.local_stack; "
        "assert 'pymysql' not in sys.modules"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal program
        [sys.executable, "-c", code],
        env={k: v for k, v in os.environ.items() if not k.upper().startswith("XIAOWEI_")},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_pymysql_import_is_scoped_to_the_lazy_tools_factory() -> None:
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = ""
            if isinstance(node, ast.Import):
                imported = node.names[0].name
            elif isinstance(node, ast.ImportFrom):
                imported = node.module or ""
            if imported.split(".", 1)[0] == "pymysql" and path.name != "starrocks.py":
                offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders


def test_pymysql_import_stays_inside_the_factory_call() -> None:
    path = _SRC / "tools" / "starrocks.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    imports = [
        node
        for node in ast.walk(tree)
        if (
            (
                isinstance(node, ast.Import)
                and node.names[0].name == "pymysql"
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.module == "pymysql.cursors"
            )
        )
    ]
    assert len(imports) == 2
    for node in imports:
        current: ast.AST | None = node
        while current is not None and not isinstance(current, ast.FunctionDef):
            current = parents.get(current)
        assert isinstance(current, ast.FunctionDef)
        assert current.name == "__call__"

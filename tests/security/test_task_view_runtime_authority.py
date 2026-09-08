"""TaskViewRuntime 与 internal-api 不得携带任务执行权。"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_TASK_VIEW_RUNTIME = _SRC / "application" / "task_view_runtime.py"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _internal_imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return {module for module in modules if module.startswith("xiaowei_agent")}


def _self_dependency_attributes(path: Path, dependency: str) -> set[str]:
    return {
        node.attr
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr == dependency
    }


def _local_stack_imports(path: Path) -> set[str]:
    return {
        alias.name
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.ImportFrom)
        and node.module == "xiaowei_agent.interfaces.local_stack"
        for alias in node.names
    }


def test_task_view_runtime_imports_no_execution_layers() -> None:
    forbidden = (
        "xiaowei_agent.governance",
        "xiaowei_agent.observability",
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
    )

    assert not {
        module
        for module in _internal_imports(_TASK_VIEW_RUNTIME)
        if module.startswith(forbidden)
    }


def test_task_view_runtime_dependency_surface_is_closed() -> None:
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_tasks") == {
        "create_task",
        "get",
    }
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_plans") == {"load"}
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_ledger") == {"load"}
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_bindings") == {
        "runtime_for_plan"
    }


def test_internal_api_and_worker_choose_different_stack_builders() -> None:
    assert _local_stack_imports(_SRC / "interfaces" / "api.py") == {
        "build_postgres_task_view_stack"
    }
    assert _local_stack_imports(_SRC / "interfaces" / "worker.py") == {
        "LocalStack",
        "build_postgres_local_stack",
    }


def test_importing_api_and_building_task_view_stack_loads_no_execution_modules() -> None:
    script = """
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces import api
from xiaowei_agent.interfaces.local_stack import build_postgres_task_view_stack

async def main():
    with TemporaryDirectory() as directory:
        secret = Path(directory) / "postgres-credential"
        secret.write_text("local-" + "fixture", encoding="utf-8")
        stack = await build_postgres_task_view_stack(
            settings=Settings(
                environment_id="dev",
                postgres_password_file=str(secret),
            )
        )
        await stack.aclose()

asyncio.run(main())

for name in (
    "xiaowei_agent.application.runtime",
    "xiaowei_agent.governance.approval",
    "xiaowei_agent.observability.durable_sink",
    "xiaowei_agent.runners.runner",
    "xiaowei_agent.runners.deterministic",
    "xiaowei_agent.tools.gateway",
    "xiaowei_agent.tools.starrocks",
    "xiaowei_agent.tools.starrocks_fake",
    "xiaowei_agent.tools.alertmanager_fake",
    "xiaowei_agent.tools.asset_inventory_fake",
    "xiaowei_agent.tools.prometheus_fake",
):
    if name in sys.modules:
        raise SystemExit(f"execution module loaded: {name}")

assert api is not None
"""

    completed = subprocess.run(  # noqa: S603 -- 当前解释器与程序均由测试控制
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

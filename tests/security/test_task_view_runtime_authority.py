"""TaskViewRuntime 与 internal-api 不得携带任务执行权。"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_TASK_VIEW_RUNTIME = _SRC / "application" / "task_view_runtime.py"

# internal-api 构建窄栈后实际加载的项目模块闭集。任何新增项都必须先经人工审查；
# 这让未知的未来模块也会转红，而不是依赖一份只能覆盖已知名字的 denylist。
_TASK_VIEW_PROCESS_ALLOWED_MODULES = frozenset(
    """
    xiaowei_agent
    xiaowei_agent.application
    xiaowei_agent.application.capability_input
    xiaowei_agent.application.capability_runtime
    xiaowei_agent.application.default_capabilities
    xiaowei_agent.application.model_advisory
    xiaowei_agent.application.model_ports
    xiaowei_agent.application.integration_state
    xiaowei_agent.application.task_view_runtime
    xiaowei_agent.capabilities
    xiaowei_agent.capabilities.asset_inventory
    xiaowei_agent.capabilities.effect
    xiaowei_agent.capabilities.intent
    xiaowei_agent.capabilities.prometheus_alert
    xiaowei_agent.capabilities.registry
    xiaowei_agent.capabilities.resolver
    xiaowei_agent.capabilities.specs
    xiaowei_agent.capabilities.target
    xiaowei_agent.config
    xiaowei_agent.contracts
    xiaowei_agent.contracts.answerability
    xiaowei_agent.contracts.approval
    xiaowei_agent.contracts.base
    xiaowei_agent.contracts.candidates
    xiaowei_agent.contracts.capability
    xiaowei_agent.contracts.channel
    xiaowei_agent.contracts.clarification
    xiaowei_agent.contracts.disclosure
    xiaowei_agent.contracts.enums
    xiaowei_agent.contracts.errors
    xiaowei_agent.contracts.evidence
    xiaowei_agent.contracts.external
    xiaowei_agent.contracts.external_input
    xiaowei_agent.contracts.ids
    xiaowei_agent.contracts.integration_config
    xiaowei_agent.contracts.intent
    xiaowei_agent.contracts.interaction
    xiaowei_agent.contracts.model
    xiaowei_agent.contracts.plan
    xiaowei_agent.contracts.policy
    xiaowei_agent.contracts.promql_surface
    xiaowei_agent.contracts.provider_state
    xiaowei_agent.contracts.readiness
    xiaowei_agent.contracts.render
    xiaowei_agent.contracts.request
    xiaowei_agent.contracts.sql_surface
    xiaowei_agent.contracts.target
    xiaowei_agent.contracts.task
    xiaowei_agent.contracts.tool
    xiaowei_agent.contracts.trace_events
    xiaowei_agent.evidence
    xiaowei_agent.evidence.asset_inventory
    xiaowei_agent.evidence.builder
    xiaowei_agent.evidence.errors
    xiaowei_agent.evidence.prometheus_alert
    xiaowei_agent.governance
    xiaowei_agent.governance.admission
    xiaowei_agent.governance.binding
    xiaowei_agent.governance.profiles
    xiaowei_agent.interfaces
    xiaowei_agent.interfaces.api
    xiaowei_agent.interfaces.auth
    xiaowei_agent.interfaces.body_limit
    xiaowei_agent.interfaces.http_models
    xiaowei_agent.interfaces.local_stack
    xiaowei_agent.interfaces.integration_config_file
    xiaowei_agent.interfaces.provider_consumption
    xiaowei_agent.log
    xiaowei_agent.persistence
    xiaowei_agent.persistence.local_admin
    xiaowei_agent.persistence.channel
    xiaowei_agent.persistence.clarification_records
    xiaowei_agent.persistence.database
    xiaowei_agent.persistence.decisions
    xiaowei_agent.persistence.errors
    xiaowei_agent.persistence.evidence
    xiaowei_agent.persistence.fake
    xiaowei_agent.persistence.memory
    xiaowei_agent.persistence.model_artifacts
    xiaowei_agent.persistence.migrations
    xiaowei_agent.persistence.migrations.runner
    xiaowei_agent.persistence.plans
    xiaowei_agent.persistence.postgres
    xiaowei_agent.persistence.provider_state
    xiaowei_agent.persistence.rows
    xiaowei_agent.persistence.schema
    xiaowei_agent.persistence.store
    xiaowei_agent.persistence.web_session
    xiaowei_agent.planning
    xiaowei_agent.planning.assets
    xiaowei_agent.planning.assets.compiler
    xiaowei_agent.planning.assets.params
    xiaowei_agent.planning.assets.slots
    xiaowei_agent.planning.canonical
    xiaowei_agent.planning.disclosure
    xiaowei_agent.planning.prometheus
    xiaowei_agent.planning.prometheus.compiler
    xiaowei_agent.planning.prometheus.params
    xiaowei_agent.planning.prometheus.slots
    xiaowei_agent.planning.prometheus.target
    xiaowei_agent.planning.prometheus.templates
    xiaowei_agent.planning.slot_verification
    xiaowei_agent.planning.starrocks
    xiaowei_agent.planning.starrocks.compiler
    xiaowei_agent.planning.starrocks.params
    xiaowei_agent.planning.starrocks.slots
    xiaowei_agent.redaction
    xiaowei_agent.reflection
    xiaowei_agent.reflection.answerability
    xiaowei_agent.reflection.asset_inventory
    xiaowei_agent.reflection.prometheus_alert
    xiaowei_agent.reflection.status
    xiaowei_agent.rendering
    xiaowei_agent.rendering.asset_inventory
    xiaowei_agent.rendering.generic
    xiaowei_agent.rendering.model_advisory
    xiaowei_agent.rendering.pending
    xiaowei_agent.rendering.prometheus_alert
    xiaowei_agent.rendering.slow_query
    xiaowei_agent.runners
    xiaowei_agent.runners.binding
    xiaowei_agent.trace
    """.split()
)


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


def _loaded_xiaowei_modules_after(script: str) -> set[str]:
    probe = script + """
import json as _json
import sys as _sys
print(_json.dumps(sorted(
    name for name in _sys.modules
    if name == "xiaowei_agent" or name.startswith("xiaowei_agent.")
)))
"""
    completed = subprocess.run(  # noqa: S603 -- 当前解释器与程序均由测试控制
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return set(json.loads(completed.stdout.splitlines()[-1]))


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
        "create_clarification_child",
        "create_task",
        "get",
        "get_submission",
    }
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_plans") == {"load"}
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_ledger") == {"load"}
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_bindings") == {
        "runtime_for_plan"
    }
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_model_artifacts") == {
        "load_advisory"
    }
    assert _self_dependency_attributes(_TASK_VIEW_RUNTIME, "_clarification_records") == {
        "load"
    }


def test_internal_api_and_worker_choose_different_stack_builders() -> None:
    assert _local_stack_imports(_SRC / "interfaces" / "api.py") == {
        "build_postgres_task_view_stack"
    }
    assert _local_stack_imports(_SRC / "interfaces" / "worker.py") == {
        "LocalStack",
        "build_postgres_local_stack",
    }


def test_importing_local_stack_does_not_load_full_application_runtime() -> None:
    loaded = _loaded_xiaowei_modules_after(
        "import xiaowei_agent.interfaces.local_stack"
    )

    assert "xiaowei_agent.application.runtime" not in loaded


def test_importing_api_and_building_task_view_stack_has_exact_module_surface() -> None:
    script = """
import asyncio
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
assert api is not None
"""
    loaded = _loaded_xiaowei_modules_after(script)

    assert loaded == set(_TASK_VIEW_PROCESS_ALLOWED_MODULES)

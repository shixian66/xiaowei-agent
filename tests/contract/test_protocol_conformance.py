"""Protocol 一致性。

**真正的 gate 在 ``src/xiaowei_agent/_conformance.py``**：验收命令是 ``mypy src``，
放在 ``tests/`` 下的带类型标注赋值根本不会被检查。本文件只做两件运行时无法交给
mypy 的事：确认锚点文件确实覆盖了所有已有实现的 Protocol；确认关键字参数没有漂移
（结构兼容性检查不看关键字名，但调用点会因此炸掉）。
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from xiaowei_agent import _conformance
from xiaowei_agent.application.capability_runtime import CapabilityBindingRegistry
from xiaowei_agent.persistence.channel import ChannelStore
from xiaowei_agent.persistence.store import TaskStore
from xiaowei_agent.runners.binding import ExecutionBindingProvider, StepEvidenceBuilder
from xiaowei_agent.runners.runner import WorkflowRunner
from xiaowei_agent.tools.adapter import ToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway

# 有实现的 Protocol 必须在锚点文件中出现；无实现的 CapabilityRegistry /
# CapabilityResolver 只冻结形状，落地实现时再补锚点。
_ANCHORED = {
    "CapabilityAssessor",
    "CapabilityPlanner",
    "CapabilityRenderer",
    "ExecutionBindingProvider",
    "FeishuIdentityDirectory",
    "FeishuInboundTransport",
    "FeishuMembershipPort",
    "StepEvidenceBuilder",
    "ToolGateway",
    "ToolAdapter",
    "TaskStore",
    "ChannelStore",
    "WorkflowRunner",
    "TraceSink",
}
_FROZEN_WITHOUT_IMPLEMENTATION = {"CapabilityRegistry", "CapabilityResolver"}


def _annotated_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
            names.add(node.annotation.id)
    return names


def test_every_implemented_protocol_has_a_typed_anchor() -> None:
    """新增 Protocol 实现却忘了加锚点时，本条转红。

    否则该 Protocol 与实现可以悄悄漂移而 mypy 毫无反应。
    """
    anchored = _annotated_names(Path(inspect.getfile(_conformance)))
    assert _ANCHORED <= anchored, f"缺少类型锚点: {sorted(_ANCHORED - anchored)}"


def test_anchor_module_has_no_runtime_import_side_effects() -> None:
    """锚点全部位于 TYPE_CHECKING 块内：不得把 fake 拉进生产导入链。"""
    tree = ast.parse(Path(inspect.getfile(_conformance)).read_text(encoding="utf-8"))
    toplevel_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    modules = {
        (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name) or ""
        for node in toplevel_imports
    }
    assert modules == {"typing"}, f"锚点模块不得在运行时导入实现: {modules}"


def _keyword_params(func: object) -> set[str]:
    return {
        name
        for name, param in inspect.signature(func).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


def test_gateway_implementation_keeps_the_protocol_keyword_arguments() -> None:
    """关键字参数漂移不会被结构兼容性检查发现，但会在调用点炸掉。"""
    assert _keyword_params(DeterministicToolGateway.invoke) == _keyword_params(
        ToolGateway.invoke
    )


def test_binding_registry_keeps_the_execution_provider_signature() -> None:
    assert _keyword_params(
        CapabilityBindingRegistry.execution_for
    ) == _keyword_params(ExecutionBindingProvider.execution_for)


def test_evidence_builders_keep_the_target_aware_protocol_signature() -> None:
    """所有生产 builder 都必须接收 Runner 已准入或漂移复核过的目标。"""
    from xiaowei_agent.application.default_capabilities import (
        PROMETHEUS_ALERT_BINDING,
        SLOW_QUERY_BINDING,
    )

    expected = {
        "task_id",
        "step",
        "plan",
        "target",
        "result",
        "captured_at",
    }
    assert _keyword_params(StepEvidenceBuilder.__call__) == expected
    for binding in (SLOW_QUERY_BINDING, PROMETHEUS_ALERT_BINDING):
        assert _keyword_params(binding.execution.evidence_builder) == expected


def _protocol_methods(protocol: type) -> tuple[str, ...]:
    """Protocol 自己声明的方法名。

    **不写死清单。** 写死的清单在 Protocol 长出新方法时不会失败，只会静默地少检
    一个——M4 给 TaskStore 加 list_stale_leases 时正好撞上这一点。从 Protocol 派生
    让"检查范围"随契约自动扩张。
    """
    return tuple(
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(value)
    )


@pytest.mark.parametrize(
    "implementation_path",
    [
        "xiaowei_agent.persistence.fake:InMemoryTaskStore",
        "xiaowei_agent.persistence.postgres:PostgresTaskStore",
    ],
    ids=["memory", "postgres"],
)
def test_every_task_store_implementation_keeps_the_protocol_keyword_arguments(
    implementation_path: str,
) -> None:
    """**两个实现都要检查。** 只校 fake 会让 PostgreSQL 实现的签名漂移无人发现。"""
    module_path, class_name = implementation_path.split(":")
    implementation = getattr(importlib.import_module(module_path), class_name)
    methods = _protocol_methods(TaskStore)
    # 17：M7 PR 3 增加三条纯读方法；这个数字是方法集变更哨兵。
    # 这个数字是刻意写死的哨兵：下一次扩约必须让评审者明确看到。
    assert len(methods) == 17, f"TaskStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(TaskStore, method)
        ), f"{class_name}.{method}"


@pytest.mark.parametrize(
    "implementation_path",
    [
        "xiaowei_agent.persistence.fake:InMemoryChannelStore",
        "xiaowei_agent.persistence.postgres:PostgresChannelStore",
    ],
    ids=["memory", "postgres"],
)
def test_every_channel_store_implementation_keeps_the_protocol_keyword_arguments(
    implementation_path: str,
) -> None:
    module_path, class_name = implementation_path.split(":")
    implementation = getattr(importlib.import_module(module_path), class_name)
    methods = _protocol_methods(ChannelStore)
    # 13：M7 PR 5 增加紧邻出站的 live-claim 续期；扩约必须显式过审。
    assert len(methods) == 13, f"ChannelStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(ChannelStore, method)
        ), f"{class_name}.{method}"


def test_runner_implementation_keeps_the_protocol_keyword_arguments() -> None:
    from xiaowei_agent.runners.fake import ScriptedRunner

    for method in ("start", "resume"):
        assert _keyword_params(getattr(ScriptedRunner, method)) == _keyword_params(
            getattr(WorkflowRunner, method)
        ), method


def test_the_real_runner_keeps_the_protocol_signature() -> None:
    """**真实** Runner 必须与契约逐参数一致，不只是 fake。

    只校 fake 是不够的：生产调用路径走的是 ``DeterministicStepRunner``。它一旦偏离
    契约，调用方要么跟着改签名、要么绕过 Protocol 直接调具体类——M3 一度正是后者
    （Runtime 把 runner 标成 ``object`` 加 ``type: ignore``），而那样做不会让任何
    静态检查失败。
    """
    from xiaowei_agent.runners.deterministic import DeterministicStepRunner

    for method in ("start", "resume"):
        assert inspect.signature(
            getattr(DeterministicStepRunner, method)
        ) == inspect.signature(getattr(WorkflowRunner, method)), method


def test_the_signature_check_rejects_the_old_narrow_runner() -> None:
    """反例：M2 那个窄签名必须**不**满足当前契约。

    没有这条，上面两条可能只是恰好都为真而检查本身没有分辨力。
    """
    from xiaowei_agent.contracts import ExternalInput, TaskOutcome

    class NarrowRunner:
        async def start(self, task_id: str) -> TaskOutcome: ...

        async def resume(
            self, task_id: str, external_input: ExternalInput | None = None
        ) -> TaskOutcome: ...

    assert _keyword_params(NarrowRunner.start) != _keyword_params(WorkflowRunner.start)
    assert _keyword_params(NarrowRunner.resume) != _keyword_params(
        WorkflowRunner.resume
    )


def test_protocols_without_implementations_are_documented_as_such() -> None:
    """无实现锚点的 Protocol 必须是明确列举的，而不是被遗忘的。"""
    from xiaowei_agent.capabilities import CapabilityRegistry, CapabilityResolver

    declared = {CapabilityRegistry.__name__, CapabilityResolver.__name__}
    assert declared == _FROZEN_WITHOUT_IMPLEMENTATION


def test_recording_adapter_satisfies_the_tool_adapter_protocol(
    recording_adapter: object,
) -> None:
    adapter: ToolAdapter = recording_adapter  # type: ignore[assignment]
    assert adapter is not None

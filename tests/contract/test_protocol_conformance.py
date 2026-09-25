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
from xiaowei_agent.application.model_ports import (
    InteractionClassifierPort,
    SlowQueryAdvisoryPort,
)
from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter
from xiaowei_agent.interfaces.gemini_model import GeminiModelAdapter
from xiaowei_agent.interfaces.web_auth import FeishuOAuthPort
from xiaowei_agent.persistence.activation import ActivationStore
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.channel import ChannelStore
from xiaowei_agent.persistence.clarification_records import ClarificationRecordStore
from xiaowei_agent.persistence.fake import (
    InMemoryActivationStore,
    InMemoryAdminAuditStore,
    InMemoryUserDirectoryStore,
)
from xiaowei_agent.persistence.identity import UserDirectoryStore
from xiaowei_agent.persistence.model_artifacts import ModelArtifactStore
from xiaowei_agent.persistence.postgres import (
    PostgresActivationStore,
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
)
from xiaowei_agent.persistence.store import TaskStore
from xiaowei_agent.persistence.web_session import WebSessionStore
from xiaowei_agent.runners.binding import ExecutionBindingProvider, StepEvidenceBuilder
from xiaowei_agent.runners.runner import WorkflowRunner
from xiaowei_agent.tools.adapter import ToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway

# 有实现的 Protocol 必须在锚点文件中出现；无实现的 CapabilityRegistry /
# CapabilityResolver 只冻结形状，落地实现时再补锚点。
_ANCHORED = {
    "CapabilityAssessor",
    "CapabilityAdvisoryProjector",
    "CapabilityPlanner",
    "CapabilityRenderer",
    "ExecutionBindingProvider",
    "FeishuIdentityDirectory",
    "FeishuInboundTransport",
    "FeishuOAuthPort",
    "FeishuMembershipPort",
    "StepEvidenceBuilder",
    "ToolGateway",
    "ToolAdapter",
    "TaskStore",
    "ChannelStore",
    "WorkflowRunner",
    "TraceSink",
    "WebSessionStore",
    "InteractionClassifierPort",
    "LeaseRenewalPort",
    "ClarificationRecordStore",
    "ModelArtifactStore",
    "SlowQueryAdvisoryPort",
    "UserDirectoryStore",
    "AdminAuditStore",
    "ActivationStore",
    "IntegrationConfigRepository",
    "ProbeVerdict",
}
_FROZEN_WITHOUT_IMPLEMENTATION = {"CapabilityRegistry", "CapabilityResolver"}


def _annotated_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AnnAssign):
            name = _annotation_name(node.annotation)
            if name is not None:
                names.add(name)
    return names


def _annotation_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    return None


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


def test_oauth_adapter_keeps_the_protocol_keyword_arguments() -> None:
    assert _keyword_params(FeishuOAuthAdapter.authorization_url) == _keyword_params(
        FeishuOAuthPort.authorization_url
    )
    assert _keyword_params(FeishuOAuthAdapter.exchange_code) == _keyword_params(
        FeishuOAuthPort.exchange_code
    )


def test_model_adapters_keep_both_narrow_port_signatures() -> None:
    from tests.fakes.model import ScriptedModelAdapter

    for implementation in (GeminiModelAdapter, ScriptedModelAdapter):
        assert inspect.signature(implementation.classify) == inspect.signature(
            InteractionClassifierPort.classify
        )
        assert inspect.signature(implementation.generate_advisory) == inspect.signature(
            SlowQueryAdvisoryPort.generate_advisory
        )
        assert _keyword_params(implementation.generate_advisory) == {
            "max_output_tokens"
        }


def test_model_conformance_anchor_assigns_both_real_and_fake_to_both_ports() -> None:
    tree = ast.parse(Path(inspect.getfile(_conformance)).read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_model_port_anchors"
    ]
    assert len(functions) == 1
    assignments = {
        (node.target.id, node.annotation.id)
        for node in functions[0].body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.annotation, ast.Name)
    }
    assert assignments == {
        ("real_interaction", "InteractionClassifierPort"),
        ("real_advisory", "SlowQueryAdvisoryPort"),
        ("fake_interaction", "InteractionClassifierPort"),
        ("fake_advisory", "SlowQueryAdvisoryPort"),
    }


@pytest.mark.parametrize(
    "implementation_path",
    [
        "xiaowei_agent.persistence.model_artifacts:InMemoryModelArtifactStore",
        "xiaowei_agent.persistence.postgres:PostgresModelArtifactStore",
    ],
    ids=["memory", "postgres"],
)
def test_model_artifact_stores_keep_protocol_keywords(
    implementation_path: str,
) -> None:
    module_path, class_name = implementation_path.split(":")
    implementation = getattr(importlib.import_module(module_path), class_name)
    methods = _protocol_methods(ModelArtifactStore)
    assert len(methods) == 4, f"ModelArtifactStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(ModelArtifactStore, method)
        ), f"{class_name}.{method}"


@pytest.mark.parametrize(
    "implementation_path",
    [
        "xiaowei_agent.persistence.clarification_records:InMemoryClarificationRecordStore",
        "xiaowei_agent.persistence.postgres:PostgresClarificationRecordStore",
    ],
    ids=["memory", "postgres"],
)
def test_clarification_record_stores_keep_protocol_keywords(
    implementation_path: str,
) -> None:
    module_path, class_name = implementation_path.split(":")
    implementation = getattr(importlib.import_module(module_path), class_name)
    methods = _protocol_methods(ClarificationRecordStore)
    assert len(methods) == 2, f"ClarificationRecordStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(ClarificationRecordStore, method)
        ), f"{class_name}.{method}"


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
    # 18：I1-A Task 1.5 增加 create_clarification_child 窄入口；这个数字是方法集变更哨兵。
    # 这个数字是刻意写死的哨兵：下一次扩约必须让评审者明确看到。
    assert len(methods) == 18, f"TaskStore 的方法集变了：{methods}"
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
    # 14：RI3 PR 3D 增加按 task 精确读取渠道绑定；15：Web 通知按主体查最近 p2p 会话
    # （飞书拒绝按 open_id 发私聊）。扩约必须显式过审。
    assert len(methods) == 15, f"ChannelStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(ChannelStore, method)
        ), f"{class_name}.{method}"


@pytest.mark.parametrize(
    "implementation_path",
    [
        "xiaowei_agent.persistence.fake:InMemoryWebSessionStore",
        "xiaowei_agent.persistence.postgres:PostgresWebSessionStore",
    ],
    ids=["memory", "postgres"],
)
def test_every_web_session_store_implementation_keeps_protocol_keywords(
    implementation_path: str,
) -> None:
    module_path, class_name = implementation_path.split(":")
    implementation = getattr(importlib.import_module(module_path), class_name)
    methods = _protocol_methods(WebSessionStore)
    # 7：连接测试 state、登录 state 各自签发/消费 + session 轮换/读取/撤销。
    assert len(methods) == 7, f"WebSessionStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(WebSessionStore, method)
        ), f"{class_name}.{method}"


def test_runner_implementation_keeps_the_protocol_keyword_arguments() -> None:
    from xiaowei_agent.runners.fake import ScriptedRunner

    for method in ("start", "resume"):
        assert _keyword_params(getattr(ScriptedRunner, method)) == _keyword_params(
            getattr(WorkflowRunner, method)
        ), method


def test_runner_protocol_declares_optional_parent_snapshot_keyword() -> None:
    for method in ("start", "resume"):
        params = inspect.signature(getattr(WorkflowRunner, method)).parameters
        assert "parent_confirmed_slots" in params
        assert params["parent_confirmed_slots"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["parent_confirmed_slots"].default is None


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


@pytest.mark.parametrize(
    "implementation_path",
    [
        "xiaowei_agent.persistence.fake:InMemoryProviderStateStore",
        "xiaowei_agent.persistence.provider_state:PostgresProviderStateStore",
    ],
    ids=["memory", "postgres"],
)
def test_every_provider_state_store_implementation_keeps_protocol_keywords(
    implementation_path: str,
) -> None:
    from xiaowei_agent.persistence.provider_state import ProviderStateStore

    module_path, class_name = implementation_path.split(":")
    implementation = getattr(importlib.import_module(module_path), class_name)
    methods = _protocol_methods(ProviderStateStore)
    # 3：整批写回执 + 写一次测试结果 + 读全量快照，扩约必须显式过审。
    assert len(methods) == 3, f"ProviderStateStore 的方法集变了：{methods}"
    for method in methods:
        assert _keyword_params(getattr(implementation, method)) == _keyword_params(
            getattr(ProviderStateStore, method)
        ), f"{class_name}.{method}"


_IDENTITY_SURFACES = (
    (ActivationStore, InMemoryActivationStore, PostgresActivationStore),
    (UserDirectoryStore, InMemoryUserDirectoryStore, PostgresUserDirectoryStore),
    (AdminAuditStore, InMemoryAdminAuditStore, PostgresAdminAuditStore),
)


def _public_surface(target: type) -> frozenset[str]:
    return frozenset(
        name
        for name in dir(target)
        if not name.startswith("_") and callable(getattr(target, name, None))
    )


@pytest.mark.parametrize(
    ("protocol", "memory", "postgres"),
    _IDENTITY_SURFACES,
    ids=lambda item: getattr(item, "__name__", str(item)),
)
def test_the_implementation_surface_equals_the_protocol_surface(
    protocol: type, memory: type, postgres: type
) -> None:
    """公开表面必须**精确等于** Protocol，用 ``==`` 而不是 ``>=``。

    ``isinstance`` 式的 Protocol 检查只保证实现**不少于**协议，方向正好相反：
    它对"多出来一个公开写方法"完全无感。而对身份目录来说，多出来的那个公开写
    方法就是绕过"授权改变必带同事务审计"的唯一途径；对审计来说，就是那个能写出
    任意候选的 ``append``。
    """
    expected = _public_surface(protocol)
    assert _public_surface(memory) == expected
    assert _public_surface(postgres) == expected


@pytest.mark.parametrize(
    ("protocol", "memory", "postgres"),
    _IDENTITY_SURFACES,
    ids=lambda item: getattr(item, "__name__", str(item)),
)
def test_identity_implementations_keep_the_protocol_keyword_arguments(
    protocol: type, memory: type, postgres: type
) -> None:
    """结构兼容性不看关键字名，但调用点会因此炸掉。"""
    for name in _public_surface(protocol):
        expected = set(inspect.signature(getattr(protocol, name)).parameters)
        for implementation in (memory, postgres):
            actual = set(inspect.signature(getattr(implementation, name)).parameters)
            assert actual == expected, f"{implementation.__name__}.{name}"


def test_file_config_adapter_is_statically_anchored_to_the_application_port() -> None:
    """W4a：文件 adapter 必须在 ``_conformance.py`` 里**赋值**给 application port。

    只靠"测试里刚好调用成功"证明不了结构兼容；这里找的是那条带注解的赋值本身。
    """
    tree = ast.parse(Path(inspect.getfile(_conformance)).read_text(encoding="utf-8"))
    assignments = {
        (_annotation_name(node.annotation), ast.unparse(node.value))
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and node.value is not None
    }
    assert ("IntegrationConfigRepository", "adapter") in assignments
    assert ("ProbeVerdict", "probe") in assignments


def test_file_config_adapter_keeps_the_port_keyword_signatures() -> None:
    from xiaowei_agent.application.integration_config_service import (
        IntegrationConfigRepository,
    )
    from xiaowei_agent.interfaces.integration_config_repository import (
        FileIntegrationConfigRepository,
    )

    for name in (
        "read_ai",
        "write_ai",
        "read_feishu",
        "write_feishu",
        "read_resources",
        "write_resources",
    ):
        assert inspect.signature(
            getattr(FileIntegrationConfigRepository, name)
        ) == inspect.signature(getattr(IntegrationConfigRepository, name)), name


def test_application_never_imports_the_interfaces_file_adapter() -> None:
    """依赖只能是 interfaces → application；反过来就是把文件原语拉进编排层。"""
    source = Path(inspect.getfile(_conformance)).parent / "application"
    offenders = sorted(
        path.name
        for path in source.rglob("*.py")
        if "xiaowei_agent.interfaces" in path.read_text(encoding="utf-8")
    )
    assert offenders == []

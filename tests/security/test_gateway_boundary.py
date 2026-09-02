"""ToolGateway 是数据面唯一工具入口，且 M0-M7 的 E1 调用次数恒为 0。"""

import ast
import datetime as dt
from pathlib import Path

import pytest
from tests.conftest import make_certificate

from xiaowei_agent.contracts import (
    AdapterStatus,
    EffectClass,
    ErrorCategory,
    ExternalContent,
    ExternalSource,
    PolicyDecision,
    RiskLevel,
    ToolCallStatus,
)
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import _E1_EXECUTION_ENABLED, DeterministicToolGateway

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)


def test_e1_execution_is_disabled() -> None:
    """ADR-007 D7：M0-M7 全程禁止 E1，含非生产环境。开闸必须改这一行并过评审。"""
    assert _E1_EXECUTION_ENABLED is False


async def test_side_effect_call_is_refused_and_adapter_is_never_called(
    gateway, ok_call, context, recording_adapter
) -> None:
    """ADR-007 D7 承重断言 1 与 4。"""
    cert = make_certificate(ok_call, effect_class=EffectClass.MUTATE_TARGET)
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_for_another_step_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    cert = make_certificate(ok_call, step_id="s99")
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_with_tampered_args_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    """拿合法凭证替换参数执行同一步骤，必须拒绝。

    凭证是对**已准入的那个 ToolCall** 的证明，不是对步骤身份的证明。
    """
    cert = make_certificate(ok_call)
    tampered = ok_call.model_copy(update={"typed_args": {"window_minutes": 1440}})
    with pytest.raises(PermissionError):
        await gateway.invoke(tampered, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_with_tampered_timeout_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    cert = make_certificate(ok_call)
    tampered = ok_call.model_copy(update={"timeout_seconds": 300.0})
    with pytest.raises(PermissionError):
        await gateway.invoke(tampered, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_denied_policy_decision_is_refused(
    gateway, ok_call, context, recording_adapter
) -> None:
    denied = PolicyDecision(
        allow=False,
        reason_code="policy.denied",
        risk=RiskLevel.HIGH,
        policy_revision="policy-2026-09-01",
        obligations=(),
    )
    cert = make_certificate(ok_call, policy_decision=denied)
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_unregistered_adapter_fails_closed(
    gateway, ok_call, context, recording_adapter
) -> None:
    call = ok_call.model_copy(update={"gateway": "never_registered"})
    cert = make_certificate(call)
    with pytest.raises(LookupError):
        await gateway.invoke(call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


def test_gateway_requires_at_least_one_adapter() -> None:
    with pytest.raises(ValueError):
        DeterministicToolGateway(adapters={})


async def test_non_conforming_adapter_return_is_refused(ok_call, context, admission) -> None:
    """Protocol 是静态的；运行时必须自己 fail-closed。

    否则一个返回 dict 或鸭子类型对象的 adapter 能把未经契约约束的数据带进
    ToolResult——领域层拿到的"结果"再也不是 Gateway 归一化过的结果。
    """

    class _RogueAdapter:
        async def execute(self, call: object, *, context: object) -> object:
            return {"status": "ok", "payload": ({"injected": "row"},)}

    rogue = DeterministicToolGateway(adapters={"starrocks": _RogueAdapter()})
    with pytest.raises(TypeError, match="expected AdapterResponse"):
        await rogue.invoke(ok_call, context=context, admission=admission)


def test_adapter_response_cannot_carry_effect_classification() -> None:
    """adapter 在类型层面就无法降级分类（ADR-007 D7 承重断言 2）。"""
    assert not ({"side_effect", "effect_class"} & set(AdapterResponse.model_fields))


async def test_adapter_error_becomes_a_structured_agent_error(
    ok_call, context, admission
) -> None:
    """丢弃 AdapterResponse.error 会让上游失败在结果里看起来像一次空成功。"""
    cause = ExternalContent.capture(
        source=ExternalSource.TOOL,
        content="ERROR 1045 (28000): Access denied for user 'x'@'h'",
        captured_at=_AT,
    )
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=(),
                source="starrocks-fake",
                error=cause,
                elapsed_ms=7,
            ),
        )
    )
    failing = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await failing.invoke(ok_call, context=context, admission=admission)

    assert result.status is ToolCallStatus.ERROR
    assert result.error is not None
    assert result.error.category is ErrorCategory.UPSTREAM
    assert result.error.cause_ref == cause.digest


async def test_adapter_error_text_never_reaches_the_tool_result(
    ok_call, context, admission
) -> None:
    """只有摘要引用出去，原文留在 ExternalContent 里。"""
    # S105 误报：这是被测的上游错误原文，不是凭证。
    upstream_text = "Access denied for user 'admin'"
    cause = ExternalContent.capture(
        source=ExternalSource.TOOL, content=upstream_text, captured_at=_AT
    )
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=(),
                source="starrocks-fake",
                error=cause,
                elapsed_ms=7,
            ),
        )
    )
    failing = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await failing.invoke(ok_call, context=context, admission=admission)
    assert upstream_text not in result.model_dump_json()


async def test_failed_call_carries_no_data_view(ok_call, context, admission) -> None:
    """失败时不得把半截 payload 当作证据传出去。"""
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.ERROR,
                payload=({"partial": "row"},),
                source="starrocks-fake",
                error=None,
                elapsed_ms=7,
            ),
        )
    )
    failing = DeterministicToolGateway(adapters={"starrocks": adapter})
    result = await failing.invoke(ok_call, context=context, admission=admission)
    assert result.data_view == ()
    assert result.error is not None


def _referenced(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names}
    return names


def test_only_gateway_module_references_the_construction_witness() -> None:
    allowed = {_SRC / "tools" / "gateway.py", _SRC / "contracts" / "tool.py"}
    witness = {"_TOOL_RESULT_WITNESS", "_WITNESS_KEY"}
    offenders = [
        path.relative_to(_SRC)
        for path in _SRC.rglob("*.py")
        if path not in allowed and witness & _referenced(path)
    ]
    assert not offenders, f"以下模块不得引用构造凭证: {offenders}"


# --- 领域层禁令的正确作用域 -----------------------------------------------
_DOMAIN_PACKAGES = (
    "contracts",
    "capabilities",
    "planning",
    "governance",
    "runners",
    "observability",
)
"""只扫**领域层**。

刻意排除：
- ``tools/``：持有外部客户端正是 adapter 的本职（AGENTS.md 落点表）。
- ``persistence/``：M4 起必须引入 SQLAlchemy / 数据库驱动（DEVELOPMENT_PLAN §7 M4）。

扫描整个 ``src`` 会在 M4 与 M6b 误杀合法实现。
"""

_BANNED_ROOTS = {
    "pymysql",
    "mysql",
    "psycopg",
    "psycopg2",
    "sqlalchemy",
    "alembic",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "kafka",
    "kubernetes",
    "redis",
}


def test_domain_layer_has_no_third_party_client_import() -> None:
    offenders: list[tuple[str, str]] = []
    for package in _DOMAIN_PACKAGES:
        root = _SRC / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if isinstance(node, ast.Import):
                    module = node.names[0].name
                if (module or "").split(".")[0] in _BANNED_ROOTS:
                    offenders.append((str(path.relative_to(_SRC)), module or ""))
    assert not offenders, f"领域层不得导入外部客户端: {offenders}"

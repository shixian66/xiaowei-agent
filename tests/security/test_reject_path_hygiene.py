"""拒绝路径不得把外部/调用输入拼进异常消息。

这是**全项目不变量**，不是某一层的实现细节。上一轮我在契约层把它当作 pydantic
的问题解决了（``hide_input_in_errors`` + 映射键改序号定位），却没有把它提升为
不变量——于是 Gateway、canonical、plan 里手写的 ``raise`` 全部漏网：

    adapter not registered: password=hunter2
    canonical key collision after NFC: 'é-password=hunter2'

逐个改这些消息治不了根：下一个新增的 ``raise`` 会以同样方式回显。因此这里把
"``raise`` 里允许插入什么"变成一张显式白名单，新增任何其他插值都必须报红，
作者被迫做一次决定（改成序号/错误码，或把理由写进白名单）。

与 ``PLAN_FIELD_TO_HASH_KEY`` 之类的映射表同一个模式：用机制保证覆盖完整，
而不是靠每次记得。
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.security

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

SAFE_INTERPOLATIONS: frozenset[str] = frozenset(
    {
        # --- 类型名：是类的身份，不是数据 ---
        "type(value).__name__",
        "type(response).__name__",
        # --- 序号与模块常量：不含任何调用方取值 ---
        "index",
        "label",
        "_DETAIL_VALUE_MAX",
        # --- 枚举成员：取值域是代码里的闭集 ---
        "self.kind.value",
        "self.status",
        "approval.state.value",
        "outcome_status.value",
        "result.rejection",
        # --- 闭集：来自代码定义的必填操作数名，不是外部输入 ---
        "sorted(required)",
        # --- 已经过安全投影 ---
        # config.py 的 detail 来自 redaction.safe_error_details，只含 loc 与 type。
        "detail",
        # --- 显式豁免（环境变量名，非取值）---
        # 未知配置变量的**名字**是 fail-fast 诊断的全部价值所在，而 secret 存在
        # 取值里、不在名字里；环境变量名也不可能包含 "="。这是一次有意识的决定，
        # 不是遗漏——因此写在这里而不是被静默放过。
        "unknown",
    }
)


def _raise_interpolations() -> list[tuple[str, str]]:
    """返回 ``raise`` 语句消息里出现的全部插值表达式 (位置, 源文本)。"""
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.JoinedStr):
                    found.extend(
                        (f"{path.relative_to(SRC)}:{node.lineno}", ast.unparse(part.value))
                        for part in sub.values
                        if isinstance(part, ast.FormattedValue)
                    )
                elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
                    found.append((f"{path.relative_to(SRC)}:{node.lineno}", "%-format"))
                elif (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "format"
                ):
                    found.append((f"{path.relative_to(SRC)}:{node.lineno}", ".format()"))
    return found


def test_no_raise_message_interpolates_unvetted_values() -> None:
    offenders = [
        f"{loc}  ->  {expr}"
        for loc, expr in _raise_interpolations()
        if expr not in SAFE_INTERPOLATIONS
    ]
    assert offenders == [], (
        "以下 raise 消息插入了未经审阅的表达式。拒绝路径不得回显外部/调用输入：\n"
        + "\n".join(offenders)
        + "\n改用序号或错误码定位；确属安全时把表达式加进 SAFE_INTERPOLATIONS 并写明理由。"
    )


def test_allowlist_has_no_dead_entries() -> None:
    """白名单不得留下已不存在的条目。

    死条目会悄悄放宽护栏：一个被删掉的表达式留在名单里，下次有人写出同名表达式
    时就会被静默放行。与 hash 映射表必须与 model_fields 相等是同一条要求。
    """
    live = {expr for _loc, expr in _raise_interpolations()}
    assert SAFE_INTERPOLATIONS - live == set()


def test_detector_catches_each_interpolation_form() -> None:
    """检测器自测：三种字符串拼接形式都要抓到，普通字符串不得误报。"""
    import tempfile

    cases = {
        'raise ValueError(f"bad: {secret}")': "secret",
        'raise ValueError("bad: %s" % secret)': "%-format",
        'raise ValueError("bad: {}".format(secret))': ".format()",
    }
    for source, expected in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            probe = pathlib.Path(tmp) / "probe.py"
            probe.write_text(source + chr(10), encoding="utf-8")
            tree = ast.parse(probe.read_text(encoding="utf-8"))
            exprs: list[str] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.JoinedStr):
                        exprs += [
                            ast.unparse(part.value)
                            for part in sub.values
                            if isinstance(part, ast.FormattedValue)
                        ]
                    elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
                        exprs.append("%-format")
                    elif (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "format"
                    ):
                        exprs.append(".format()")
            assert expected in exprs, source


def test_constant_messages_are_not_flagged() -> None:
    """反例：纯常量消息不得被算作插值，否则护栏会逼人放弃可读的错误文本。"""
    tree = ast.parse('raise ValueError("plan exceeds max_steps budget")' + chr(10))
    joined = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    assert joined == []


# --- 运行时 canary：静态扫描保证不新增，这里保证确实不泄漏 -------------------

import asyncio  # noqa: E402
import unicodedata  # noqa: E402

from tests.conftest import make_certificate  # noqa: E402
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TOOL_CALL  # noqa: E402

from xiaowei_agent.contracts import RequestContext  # noqa: E402
from xiaowei_agent.planning import canonical_json  # noqa: E402
from xiaowei_agent.tools.gateway import DeterministicToolGateway  # noqa: E402

CANARY = "synthetic-canary-password=hunter2"


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


def test_unregistered_adapter_message_carries_no_gateway_name() -> None:
    call = FIXTURE_TOOL_CALL.model_copy(update={"gateway": CANARY})
    gateway = DeterministicToolGateway(adapters={"registered": object()})
    with pytest.raises(LookupError) as caught:
        asyncio.run(
            gateway.invoke(call, context=_context(), admission=make_certificate(call))
        )
    assert CANARY not in str(caught.value)


def test_canonical_key_collision_message_carries_no_key() -> None:
    composed = unicodedata.normalize("NFC", f"é-{CANARY}")
    decomposed = unicodedata.normalize("NFD", f"é-{CANARY}")
    assert composed != decomposed  # 前提：两者确实是不同的码点序列
    with pytest.raises(ValueError) as caught:
        canonical_json({composed: 1, decomposed: 2})
    assert CANARY not in str(caught.value)


def test_plan_validator_messages_carry_no_step_id() -> None:
    """重复 step_id 与未知依赖两条路径都不得回显 id。"""
    from pydantic import ValidationError

    poisoned = FIXTURE_PLAN.steps[0].model_copy(update={"step_id": CANARY})
    with pytest.raises(ValidationError) as caught:
        FIXTURE_PLAN.model_copy(update={"steps": (poisoned, poisoned)})
    assert CANARY not in str(caught.value)

    dangling = FIXTURE_PLAN.steps[0].model_copy(update={"depends_on": (CANARY,)})
    with pytest.raises(ValidationError) as caught:
        FIXTURE_PLAN.model_copy(update={"steps": (dangling,)})
    assert CANARY not in str(caught.value)

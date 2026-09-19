"""固定输入必须产生逐字节相同的计划。

这是 M3 的第一条退出标准，也是审批绑定成立的前提：``plan_hash`` 若随调用时刻、
槽位措辞或遍历顺序变化，一份已批准的计划在恢复时就会被判成漂移；反过来，若
某个参数**不**进 hash，一份已批准的计划就能执行另一条查询。
"""

import ast
import datetime as dt
from pathlib import Path

import pytest

from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.specs import (
    CAPABILITY_ID,
    OP_LIST,
)
from xiaowei_agent.capabilities.specs import (
    SLOW_QUERY_SURFACE as SURFACE,
)
from xiaowei_agent.capabilities.target import resolve_target
from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext
from xiaowei_agent.planning import compute_plan_hash
from xiaowei_agent.planning.starrocks import compiler
from xiaowei_agent.planning.starrocks.compiler import compile_plan
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

pytestmark = pytest.mark.security

SNAPSHOT = StaticCapabilityRegistry().snapshot()
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)

_PARAMS = SlowQueryParams(
    window_start=dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC),
    min_query_time_ms=10_000,
    row_limit=20,
)


def _draft(**slots: str) -> IntentDraft:
    return IntentDraft(
        intent=CAPABILITY_ID,
        slots=slots,
        missing=(),
        confidence=0.9,
        source=IntentSource.USER,
    )


def _plan_for(draft: IntentDraft, *, context: RequestContext = CONTEXT) -> object:
    candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=draft, context=context, snapshot=SNAPSHOT)
        .items
        if item.operation == OP_LIST
    )
    return compile_plan(
        candidate=candidate,
        params=_PARAMS,
        target=resolve_target(context=context, params=_PARAMS),
        context=context,
        snapshot=SNAPSHOT,
        surface=SURFACE,
    )


def test_repeated_compilation_is_byte_identical() -> None:
    first, second = _plan_for(_draft()), _plan_for(_draft())
    assert first == second
    assert compute_plan_hash(first) == compute_plan_hash(second)
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize(
    "hostile",
    [
        {"sql": "DROP TABLE t"},
        {"operation": "mutate_probe"},
        {"capability_id": "test.synthetic.write"},
        {"effect_class": "mutate_target"},
        {"side_effect": "true"},
        {"policy_profile": "write.synthetic"},
        {"approval_ref": "a1"},
    ],
)
def test_hostile_slots_never_change_the_plan(hostile: dict[str, str]) -> None:
    """A25：模型往槽位里塞执行权字段，编译出的计划必须逐字节不变。

    槽位在解释器那一层就已经被闭集挡掉；这里再从**计划产物**这一端断言一次，
    因为两层的失效方式不同：解释器可能被换成别的实现，而这条断言看的是结果。
    """
    baseline = compute_plan_hash(_plan_for(_draft()))
    assert compute_plan_hash(_plan_for(_draft(**hostile))) == baseline


def test_actor_does_not_change_the_plan_hash() -> None:
    """发起人不进计划：同一份计划不应因换人发起而被判成漂移。"""
    baseline = compute_plan_hash(_plan_for(_draft()))
    other = CONTEXT.model_copy(update={"actor": "bob"})
    assert compute_plan_hash(_plan_for(_draft(), context=other)) == baseline


def test_policy_revision_changes_the_plan_hash() -> None:
    """policy 变化不能静默让旧审批继续生效（ARCHITECTURE §15）。"""
    baseline = compute_plan_hash(_plan_for(_draft()))
    newer = CONTEXT.model_copy(update={"policy_revision": "policy-2026-10-01"})
    assert compute_plan_hash(_plan_for(_draft(), context=newer)) != baseline


def test_no_step_carries_a_capability_identifier() -> None:
    """步骤不得携带 capability 标识（ADR-009 D2）。

    若步骤各带一份而 plan_hash 的 ordered_steps 不含它们，同一份计划就能改掉
    某步的 capability 而 hash 不变。
    """
    for step in _plan_for(_draft()).steps:
        assert not ({"capability_id", "capability_version"} & set(step.typed_arguments))


def test_plan_carries_no_execution_context() -> None:
    """租户、发起人、trace 不进计划：它们由 target_fingerprint 与审计承载。"""
    dumped = _plan_for(_draft()).model_dump_json()
    assert "alice" not in dumped
    assert CONTEXT.trace_id not in dumped


def test_compile_plan_does_not_construct_plan_step_directly() -> None:
    """由 test_only_effect_module_constructs_plan_step 全局承重。

    这里再钉一条本模块的局部断言，使违规在本任务内就被发现，而不是等到全量安全 gate。
    """
    tree = ast.parse(Path(compiler.__file__).read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "PlanStep" not in calls
    assert "build_plan_step" in calls

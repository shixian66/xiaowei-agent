"""side_effect / effect_class 只能由版本化 CapabilitySpec 确定性派生。

逐条对应 ADR-007 D7 的四条 fail-closed 规则与承重断言。
"""

import inspect

import pytest
from tests.fakes.fixtures import CAP_VERSION, READ_CAP, READ_OP, SNAPSHOT, WRITE_CAP, WRITE_OP

from xiaowei_agent.capabilities import (
    SpecResolutionError,
    build_plan_step,
    derive_effect,
    verify_plan_effects,
)
from xiaowei_agent.contracts import (
    EffectClass,
    ExecutionPlan,
    PlanBudget,
    PlanStep,
    ReadClass,
)

pytestmark = pytest.mark.security

_BUDGET = PlanBudget(max_steps=4, max_tool_calls=4, max_model_tokens=8000)


def _plan(*steps: PlanStep, capability_id: str = READ_CAP) -> ExecutionPlan:
    return ExecutionPlan(
        capability_id=capability_id,
        capability_version=CAP_VERSION,
        steps=steps,
        policy_profile="readonly.default",
        policy_revision="policy-2026-09-01",
        budget=_BUDGET,
    )


def _forged(**overrides: object) -> PlanStep:
    base: dict[str, object] = {
        "step_id": "s1",
        "operation": READ_OP,
        "typed_arguments": {},
        "depends_on": (),
        "side_effect": False,
        "effect_class": EffectClass.READ,
        "read_class": ReadClass.BOUNDED,
    }
    return PlanStep(**(base | overrides))


# --- 规则 1 与 4：分类未知、未声明、版本不可确定 --------------------------
def test_undeclared_operation_fails_closed() -> None:
    with pytest.raises(SpecResolutionError):
        derive_effect(
            SNAPSHOT,
            capability_id=READ_CAP,
            capability_version=CAP_VERSION,
            operation="never_declared",
        )


def test_unknown_capability_fails_closed() -> None:
    with pytest.raises(SpecResolutionError):
        derive_effect(
            SNAPSHOT,
            capability_id="no.such.capability",
            capability_version=CAP_VERSION,
            operation=READ_OP,
        )


def test_unknown_capability_version_fails_closed() -> None:
    """版本不可确定时不回退到任意版本。"""
    with pytest.raises(SpecResolutionError):
        derive_effect(
            SNAPSHOT,
            capability_id=READ_CAP,
            capability_version="9.9.9",
            operation=READ_OP,
        )


def test_effect_class_has_no_unknown_member() -> None:
    """枚举无 UNKNOWN 成员，因此"未知"在解析层即失败，不存在按只读放行的取值。"""
    assert {m.value for m in EffectClass} == {"read", "mutate_target"}


# --- 规则 2 与 3：声明冲突、写操作误标只读 --------------------------------
def test_write_operation_forged_as_readonly_is_rejected() -> None:
    """ADR-007 D7 承重断言 1。"""
    plan = _plan(
        _forged(operation=WRITE_OP, side_effect=False, effect_class=EffectClass.READ),
        capability_id=WRITE_CAP,
    )
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


def test_effect_class_conflicting_with_spec_is_rejected() -> None:
    plan = _plan(
        _forged(operation=WRITE_OP, side_effect=True, effect_class=EffectClass.READ),
        capability_id=WRITE_CAP,
    )
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


def test_side_effect_mismatch_alone_is_rejected() -> None:
    """只让 side_effect 不符、effect_class 相符，单独承重那一条检查。

    其他伪造用例里 effect_class 总是先被抓住，side_effect 的比对从未被单独证明
    有效——变异测试（移除该比对仍全绿）暴露了这个覆盖缺口。
    """
    plan = _plan(
        _forged(
            operation=WRITE_OP,
            side_effect=False,
            effect_class=EffectClass.MUTATE_TARGET,
            read_class=None,
        ),
        capability_id=WRITE_CAP,
    )
    with pytest.raises(SpecResolutionError, match="side_effect mismatch"):
        verify_plan_effects(SNAPSHOT, plan)


def test_effect_class_mismatch_alone_is_rejected() -> None:
    """对称的另一半：只让 effect_class 不符、side_effect 相符。"""
    plan = _plan(
        _forged(operation=WRITE_OP, side_effect=True, effect_class=EffectClass.READ),
        capability_id=WRITE_CAP,
    )
    with pytest.raises(SpecResolutionError, match="effect_class mismatch"):
        verify_plan_effects(SNAPSHOT, plan)


def test_readonly_operation_forged_as_write_is_rejected() -> None:
    """反向伪造同样拒绝：提权不比降权更可接受。"""
    plan = _plan(
        _forged(
            side_effect=True,
            effect_class=EffectClass.MUTATE_TARGET,
            read_class=None,
        )
    )
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


def test_read_class_mismatch_is_rejected() -> None:
    plan = _plan(_forged(read_class=ReadClass.RESTRICTED))
    with pytest.raises(SpecResolutionError, match="read_class mismatch"):
        verify_plan_effects(SNAPSHOT, plan)


def test_every_step_is_verified_not_just_the_first() -> None:
    """按单步校验要求调用方对每一步都记得调用，漏一步就是缺口。"""
    good = build_plan_step(
        SNAPSHOT,
        capability_id=READ_CAP,
        capability_version=CAP_VERSION,
        operation=READ_OP,
        step_id="s1",
        typed_arguments={},
    )
    bad = _forged(
        step_id="s2",
        side_effect=True,
        effect_class=EffectClass.MUTATE_TARGET,
        read_class=None,
    )
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, _plan(good, bad))


def test_matching_plan_passes() -> None:
    step = build_plan_step(
        SNAPSHOT,
        capability_id=READ_CAP,
        capability_version=CAP_VERSION,
        operation=READ_OP,
        step_id="s1",
        typed_arguments={"window_minutes": 30},
    )
    verify_plan_effects(SNAPSHOT, _plan(step))


# --- 唯一构造器 ----------------------------------------------------------
def test_build_plan_step_derives_classification_from_the_snapshot() -> None:
    step = build_plan_step(
        SNAPSHOT,
        capability_id=READ_CAP,
        capability_version=CAP_VERSION,
        operation=READ_OP,
        step_id="s1",
        typed_arguments={"window_minutes": 30},
    )
    assert step.effect_class is EffectClass.READ
    assert step.read_class is ReadClass.BOUNDED
    assert step.side_effect is False


def test_build_plan_step_accepts_no_classification_override() -> None:
    """调用方无从传入分类：它不是参数（ADR-007 D7 承重断言 2）。"""
    params = set(inspect.signature(build_plan_step).parameters)
    assert not ({"side_effect", "effect_class", "read_class"} & params)


def test_build_plan_step_fails_closed_on_an_undeclared_operation() -> None:
    with pytest.raises(SpecResolutionError):
        build_plan_step(
            SNAPSHOT,
            capability_id=READ_CAP,
            capability_version=CAP_VERSION,
            operation="never_declared",
            step_id="s1",
            typed_arguments={},
        )

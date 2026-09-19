"""计划契约的结构完整性。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AdmissionCertificate,
    ApprovalRequest,
    EffectClass,
    ExecutionPlan,
    PlanBudget,
    PlanStep,
    ReadClass,
    StepCondition,
    StepConditionKind,
    StepResultStatus,
)
from xiaowei_agent.contracts.plan import PLAN_SCHEMA_VERSION

_BUDGET = PlanBudget(max_steps=2, max_tool_calls=2, max_model_tokens=8000)


def _step(step_id: str = "s1", **overrides: object) -> PlanStep:
    base: dict[str, object] = {
        "step_id": step_id,
        "operation": "list_slow_queries",
        "typed_arguments": {"window_minutes": 30},
        "depends_on": (),
        "side_effect": False,
        "effect_class": EffectClass.READ,
        "read_class": ReadClass.BOUNDED,
    }
    return PlanStep(**(base | overrides))


def _plan(
    *steps: PlanStep, budget: PlanBudget = _BUDGET, **overrides: object
) -> ExecutionPlan:
    base: dict[str, object] = {
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "steps": steps,
        "policy_profile": "readonly.default",
        "policy_revision": "policy-2026-09-01",
        "budget": budget,
    }
    return ExecutionPlan(**(base | overrides))


def test_plan_step_carries_no_capability_fields() -> None:
    """计划绑定单 capability；step 级 capability 字段会造成 hash 覆盖缺口。"""
    assert not ({"capability_id", "capability_version"} & set(PlanStep.model_fields))


def test_execution_plan_has_no_self_declared_hash_fields() -> None:
    assert not ({"plan_hash", "target_fingerprint"} & set(ExecutionPlan.model_fields))


def test_plan_schema_version_is_v2_for_read_class_hash_coverage() -> None:
    assert PLAN_SCHEMA_VERSION == 2


def test_old_plan_schema_version_is_rejected_fail_closed() -> None:
    with pytest.raises(ValidationError, match="plan_schema_version"):
        _plan(_step(), plan_schema_version=1)


def test_read_step_requires_read_class() -> None:
    with pytest.raises(ValidationError, match="read_class"):
        _step(read_class=None)


def test_non_read_step_must_not_have_read_class() -> None:
    with pytest.raises(ValidationError, match="read_class"):
        _step(
            effect_class=EffectClass.MUTATE_TARGET,
            side_effect=True,
            read_class=ReadClass.BOUNDED,
        )


def test_duplicate_step_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate step_id"):
        _plan(_step("s1"), _step("s1"))


def test_unknown_dependency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(_step("s1", depends_on=("nope",)))


def test_forward_dependency_is_rejected() -> None:
    """只能依赖序列中更早的步骤，因此环与前向引用在校验期即失败。"""
    with pytest.raises(ValidationError):
        _plan(_step("s1", depends_on=("s2",)), _step("s2"))


def test_backward_dependency_is_accepted() -> None:
    plan = _plan(_step("s1"), _step("s2", depends_on=("s1",)))
    assert plan.steps[1].depends_on == ("s1",)


def test_condition_referencing_a_later_step_is_rejected() -> None:
    later = StepCondition(
        kind=StepConditionKind.PRIOR_STEP_RESULT_IS,
        ref_step_id="s2",
        expected_result=StepResultStatus.FAILED,
    )
    with pytest.raises(ValidationError):
        _plan(_step("s1", condition=later), _step("s2"))


def test_condition_referencing_an_earlier_step_is_accepted() -> None:
    earlier = StepCondition(
        kind=StepConditionKind.PRIOR_STEP_RESULT_IS,
        ref_step_id="s1",
        expected_result=StepResultStatus.FAILED,
    )
    plan = _plan(_step("s1"), _step("s2", condition=earlier))
    assert plan.steps[1].condition.ref_step_id == "s1"


def test_step_count_exceeding_budget_is_rejected() -> None:
    tight = PlanBudget(max_steps=1, max_tool_calls=1, max_model_tokens=100)
    with pytest.raises(ValidationError, match="max_steps"):
        _plan(_step("s1"), _step("s2"), budget=tight)


def test_typed_arguments_reject_nested_structures() -> None:
    """参数只允许 JSON 标量；嵌套结构会成为夹带任意 SQL 或代码的通道。"""
    with pytest.raises(ValidationError):
        _step(typed_arguments={"filter": {"db": "prod"}})


def test_typed_arguments_are_deeply_immutable() -> None:
    step = _step()
    with pytest.raises(TypeError):
        step.typed_arguments["window_minutes"] = 1440  # type: ignore[index]


@pytest.mark.parametrize("bad", [0, -1, True, "4"])
def test_budget_must_be_a_positive_strict_int(bad: object) -> None:
    with pytest.raises(ValidationError):
        PlanBudget(max_steps=bad, max_tool_calls=1, max_model_tokens=1)


def test_copy_cannot_create_a_forward_dependency() -> None:
    plan = _plan(_step("s1"))
    bad = plan.steps[0].model_copy(update={"depends_on": ("s99",)})
    with pytest.raises(ValidationError):
        plan.model_copy(update={"steps": (bad,)})


def test_copy_cannot_create_a_non_positive_budget() -> None:
    with pytest.raises(ValidationError):
        _BUDGET.model_copy(update={"max_tool_calls": 0})


def test_admission_certificate_requires_tool_call_hash() -> None:
    """凭证必须绑定调用内容，而不只是步骤身份。"""
    assert "tool_call_hash" in AdmissionCertificate.model_fields


def test_approval_request_carries_policy_revision() -> None:
    """policy 变化不能静默让旧审批继续生效。"""
    assert "policy_revision" in ApprovalRequest.model_fields

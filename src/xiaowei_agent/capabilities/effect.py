"""E1 分类的唯一确定性来源（ADR-007 D7）。

三个函数分工：

- :func:`derive_effect` 从版本化快照读出声明，是分类的唯一来源。
- :func:`build_plan_step` 是全项目唯一被允许构造带分类字段 ``PlanStep`` 的入口；
  它**不接受**分类参数，调用方无从设置、覆盖或降级。
- :func:`verify_plan_effects` 在准入前重算并核对**计划的每一个步骤**。

**为什么保证放在"每次使用前重算"而不是"构造时正确"**：计划会经 TaskStore 往返、
进程重启与并发抢占，只有重算才能覆盖持久化之后被篡改的情形。

**为什么校验整个计划而不是单步**：单步版本要求调用方对每一步都记得调用，漏掉一步
就出现缺口；整计划版本没有这个漏项面。
"""

from collections.abc import Mapping

from xiaowei_agent.contracts import (
    CapabilitySnapshot,
    ExecutionPlan,
    JsonScalar,
    OperationSpec,
    PlanStep,
    StepCondition,
)


class SpecResolutionError(RuntimeError):
    """分类来源缺失、版本不可确定或声明与步骤标记冲突。一律 fail-closed。"""


def derive_effect(
    snapshot: CapabilitySnapshot,
    *,
    capability_id: str,
    capability_version: str,
    operation: str,
) -> OperationSpec:
    """从版本化快照确定性派生 operation 的分类声明。

    :raises SpecResolutionError: 能力、版本或 operation 任一无法唯一确定。
    """
    for spec in snapshot.specs:
        if spec.capability_id != capability_id or spec.version != capability_version:
            continue
        for op in spec.operations:
            if op.operation == operation:
                return op
        # 不回显 capability_id / version / operation：这三个正是本次未能在快照中
        # 匹配上的输入，属于未经校验的调用方数据。调用方本就持有它们。
        raise SpecResolutionError("operation is not declared by the resolved capability")
    raise SpecResolutionError("capability version is not present in the snapshot")


def build_plan_step(
    snapshot: CapabilitySnapshot,
    *,
    capability_id: str,
    capability_version: str,
    operation: str,
    step_id: str,
    typed_arguments: Mapping[str, JsonScalar],
    depends_on: tuple[str, ...] = (),
    condition: StepCondition | None = None,
) -> PlanStep:
    """构造计划步骤；分类字段由快照派生，**不接受调用方传入**。"""
    declared = derive_effect(
        snapshot,
        capability_id=capability_id,
        capability_version=capability_version,
        operation=operation,
    )
    return PlanStep(
        step_id=step_id,
        operation=operation,
        typed_arguments=typed_arguments,
        depends_on=depends_on,
        side_effect=declared.side_effect,
        effect_class=declared.effect_class,
        read_class=declared.read_class,
        condition=condition if condition is not None else StepCondition(),
    )


def verify_plan_effects(snapshot: CapabilitySnapshot, plan: ExecutionPlan) -> None:
    """重算并核对计划中**每一个**步骤的分类标记；任一不符即拒绝。

    :raises SpecResolutionError: 分类无法派生，或与步骤标记不一致（双向皆拒）。
    """
    for index, step in enumerate(plan.steps):
        declared = derive_effect(
            snapshot,
            capability_id=plan.capability_id,
            capability_version=plan.capability_version,
            operation=step.operation,
        )
        if step.effect_class is not declared.effect_class:
            raise SpecResolutionError(f"effect_class mismatch on step #{index}")
        if step.side_effect != declared.side_effect:
            raise SpecResolutionError(f"side_effect mismatch on step #{index}")
        if step.read_class is not declared.read_class:
            raise SpecResolutionError(f"read_class mismatch on step #{index}")

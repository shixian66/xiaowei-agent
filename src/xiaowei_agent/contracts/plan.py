"""执行计划。

**计划绑定单一 capability**：``capability_id`` / ``capability_version`` 只出现在
计划级，不出现在步骤级。若步骤各带一份而 ``plan_hash`` 的 ordered_steps 不含
它们，同一份计划就能改掉某步的 capability 而 hash 不变（ADR-009 D2）。跨
capability 计划须递增 ``PLAN_SCHEMA_VERSION`` 并另立 ADR。

**闭集条件是可选只读分支的全部安全价值**：``StepConditionKind`` 只有四个成员，
因此"条件"无法表达任意谓词、无法引用模型输出、无法承载代码。要新增一种条件必须
改这个枚举并通过评审，无法在运行时构造（ARCHITECTURE §4.2）。

``plan_hash`` / ``target_fingerprint`` **不是本类的字段**：存一份"自称的 hash"
只会制造可被篡改的第二真源。两者由 ``planning`` 按需计算，绑定值只存在于
``ApprovalRequest``，恢复时重算比对（ADR-009 D3、ARCHITECTURE §5.7）。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, FrozenMap, StrictInt, StrictStr
from xiaowei_agent.contracts.enums import EffectClass, StepConditionKind, StepResultStatus

PLAN_SCHEMA_VERSION: Final[int] = 1
"""含 effect_class、condition 与 budget 的首个规范形状（ADR-009 D1）。

计划字段新增或语义变化必须递增本常量，并同步更新 ARCHITECTURE.md §7.1 与
``planning`` 的「字段 → 指纹键」映射表。
"""

_OPERANDS: Final[Mapping[StepConditionKind, frozenset[str]]] = MappingProxyType(
    {
        StepConditionKind.ALWAYS: frozenset(),
        StepConditionKind.EVIDENCE_FIELD_ABSENT: frozenset({"ref_step_id", "field"}),
        StepConditionKind.EVIDENCE_ROW_COUNT_BELOW: frozenset({"ref_step_id", "threshold"}),
        StepConditionKind.PRIOR_STEP_RESULT_IS: frozenset({"ref_step_id", "expected_result"}),
    }
)

_OPERAND_NAMES: Final[tuple[str, ...]] = (
    "ref_step_id",
    "field",
    "threshold",
    "expected_result",
)


class StepCondition(Contract):
    kind: StepConditionKind = StepConditionKind.ALWAYS
    ref_step_id: StrictStr | None = None
    field: StrictStr | None = None
    threshold: StrictInt | None = Field(default=None, ge=0)
    expected_result: StepResultStatus | None = None

    @model_validator(mode="after")
    def _operands_match_kind_exactly(self) -> Self:
        present = {name for name in _OPERAND_NAMES if getattr(self, name) is not None}
        required = _OPERANDS[self.kind]
        if present != required:
            raise ValueError(f"condition {self.kind.value} requires exactly {sorted(required)}")
        return self


class PlanStep(Contract):
    step_id: StrictStr
    operation: StrictStr
    typed_arguments: FrozenMap
    depends_on: tuple[StrictStr, ...]
    side_effect: bool
    effect_class: EffectClass
    condition: StepCondition = StepCondition()


class PlanBudget(Contract):
    max_steps: StrictInt = Field(gt=0)
    max_tool_calls: StrictInt = Field(gt=0)
    max_model_tokens: StrictInt = Field(gt=0)


class ExecutionPlan(Contract):
    plan_schema_version: StrictInt = PLAN_SCHEMA_VERSION
    capability_id: StrictStr
    capability_version: StrictStr
    steps: tuple[PlanStep, ...]
    policy_profile: StrictStr
    policy_revision: StrictStr
    budget: PlanBudget

    @model_validator(mode="after")
    def _steps_are_well_formed(self) -> Self:
        """依赖只能指向序列中更早的步骤。

        ``seen`` 在遍历中逐步累积，因此前向引用与环在校验期即失败，无需事后做
        拓扑排序；也让"步骤顺序即执行顺序"成为契约的一部分——``plan_hash`` 的
        ``ordered_steps`` 才有确定语义。可选分支的 ``ref_step_id`` 受同一约束：
        条件只能看已经执行过的步骤。
        """
        seen: set[str] = set()
        # 用步骤序号而不是 step_id 定位：step_id 由计划编译产生，仍是数据；
        # 拒绝路径统一不回显取值（与 canonical、Gateway、ValidationError 同一条
        # 不变量），序号在同一个 steps 元组里已经足以定位。
        for index, step in enumerate(self.steps):
            if step.step_id in seen:
                raise ValueError(f"duplicate step_id at step #{index}")
            for dep in step.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"step #{index} depends on an unknown or later step"
                    )
            ref = step.condition.ref_step_id
            if ref is not None and ref not in seen:
                raise ValueError(
                    f"step #{index} condition references an unknown or later step"
                )
            seen.add(step.step_id)
        if len(self.steps) > self.budget.max_steps:
            raise ValueError("plan exceeds max_steps budget")
        return self

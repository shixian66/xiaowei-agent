"""策略判定与策略快照。

``PolicySnapshot`` 是"当前生效的 policy revision 与允许的 profile 集合"，
使"未知 policy revision"成为可用纯函数验收的检查。
"""

from typing import Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, FiniteFloat, StrictStr
from xiaowei_agent.contracts.enums import EffectClass, ReadClass, RiskLevel


class PolicyDecision(Contract):
    allow: bool
    reason_code: StrictStr
    risk: RiskLevel
    policy_revision: StrictStr
    obligations: tuple[StrictStr, ...]


class PolicyProfile(Contract):
    """一个 policy profile 允许的操作面。

    profile 是**声明**，判定由 ``governance.policy.evaluate_tool_policy`` 做。
    把允许面写成闭集元组而不是判定函数里的 ``if``，使"这个 profile 到底允许什么"
    可以被直接读出与断言，也使新增一个允许项必须改声明并过评审。

    ``allowed_effect_classes`` 与 ``allowed_operations`` 都必须非空：空元组在
    "什么都不允许"与"忘了填"之间没有区别，而后者会以恒拒的形式伪装成很严的闸门。
    """

    profile_id: StrictStr
    allowed_operations: tuple[StrictStr, ...]
    allowed_effect_classes: tuple[EffectClass, ...]
    allowed_read_classes: tuple[ReadClass, ...] = ()
    allowed_environment_ids: tuple[StrictStr, ...]
    risk: RiskLevel
    max_timeout_seconds: FiniteFloat = Field(gt=0.0)

    @model_validator(mode="after")
    def _allowed_sets_are_non_empty_and_unique(self) -> Self:
        for label, values in (
            ("operations", self.allowed_operations),
            ("effect classes", self.allowed_effect_classes),
            ("environment ids", self.allowed_environment_ids),
        ):
            if not values:
                raise ValueError(f"profile must allow at least one of: {label}")
            if len(set(values)) != len(values):
                raise ValueError(f"duplicate entry in allowed {label}")
        if EffectClass.READ in self.allowed_effect_classes:
            if not self.allowed_read_classes:
                raise ValueError("profile allowing READ must declare read classes")
            if len(set(self.allowed_read_classes)) != len(self.allowed_read_classes):
                raise ValueError("duplicate entry in allowed read classes")
        elif self.allowed_read_classes:
            raise ValueError("non-READ profile must not declare read classes")
        return self


class PolicySnapshot(Contract):
    policy_revision: StrictStr
    profiles: tuple[StrictStr, ...]

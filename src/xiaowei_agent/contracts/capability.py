"""能力声明。

``CapabilitySpec`` 是 ``side_effect`` 与 ``effect_class`` 的**唯一确定性来源**
（ADR-007 D7）。``OperationSpec`` 在声明层就要求两者等价，使自相矛盾的声明在
**注册时**即被拒绝，而不是等到使用时才发现。

``CapabilitySnapshot`` 拒绝重复的 ``(capability_id, version)``——否则分类派生
结果会依赖遍历顺序。
"""

from typing import Self

from pydantic import model_validator

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import EffectClass


class OperationSpec(Contract):
    operation: StrictStr
    gateway: StrictStr
    effect_class: EffectClass
    side_effect: bool
    argument_schema_ref: StrictStr

    @model_validator(mode="after")
    def _side_effect_matches_effect_class(self) -> Self:
        if self.side_effect != (self.effect_class is not EffectClass.READ):
            raise ValueError("side_effect must equal (effect_class is not READ)")
        return self


class CapabilitySpec(Contract):
    capability_id: StrictStr
    version: StrictStr
    domain: StrictStr
    operations: tuple[OperationSpec, ...]
    policy_profile: StrictStr
    evidence_contract: StrictStr
    eval_ref: StrictStr

    @model_validator(mode="after")
    def _operations_are_unique_and_nonempty(self) -> Self:
        names = [op.operation for op in self.operations]
        if not names:
            raise ValueError("capability must declare at least one operation")
        if len(set(names)) != len(names):
            raise ValueError("duplicate operation in capability spec")
        return self


class CapabilitySnapshot(Contract):
    """某一时刻的已注册能力快照。Resolver 与分类派生都只看这一份。"""

    snapshot_id: StrictStr
    specs: tuple[CapabilitySpec, ...]

    @model_validator(mode="after")
    def _capability_versions_are_unique(self) -> Self:
        keys = [(s.capability_id, s.version) for s in self.specs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate (capability_id, version) in snapshot")
        return self

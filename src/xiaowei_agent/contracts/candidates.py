"""Resolver 的唯一输出。route_shadow 只消费同一份 CandidateSet，不自行 build。"""

from typing import Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, NonEmptyText, StrictStr


class Candidate(Contract):
    capability_id: StrictStr
    capability_version: StrictStr
    operation: StrictStr
    # 有界且有限：候选排序若允许 NaN，比较结果不满足全序，排序结果取决于实现
    # 细节而非数据；允许 Inf 则任一候选都能压过其余全部。
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    match_evidence: tuple[NonEmptyText, ...]
    required_context: tuple[StrictStr, ...]


class Rejection(Contract):
    """被拒绝的候选。

    粒度必须与 :class:`Candidate` 一致（三元组含 ``operation``）：若拒绝只到
    capability/version 级，同一 capability 的一个 operation 被拒就会与另一个
    operation 的候选冲突，"这个能力到底是候选还是被拒"没有确定答案。
    """

    capability_id: StrictStr
    capability_version: StrictStr
    operation: StrictStr
    reason_code: StrictStr


class CandidateSet(Contract):
    resolver_version: StrictStr
    snapshot_id: StrictStr
    items: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]

    @model_validator(mode="after")
    def _candidates_are_unambiguous(self) -> Self:
        """同一目标不得重复出现，也不得既是候选又被拒绝。

        Resolver 是唯一候选真源；同一目标出现两条记录时，"选中了哪一条"就依赖
        遍历顺序，shadow 对比也随之失去意义。
        """
        keys = [(c.capability_id, c.capability_version, c.operation) for c in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate candidate in CandidateSet")
        rejected = [
            (r.capability_id, r.capability_version, r.operation) for r in self.rejections
        ]
        if len(set(rejected)) != len(rejected):
            raise ValueError("duplicate rejection in CandidateSet")
        if set(rejected) & set(keys):
            raise ValueError("operation appears as both candidate and rejection")
        return self

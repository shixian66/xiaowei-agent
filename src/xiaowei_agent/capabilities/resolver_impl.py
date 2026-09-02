"""确定性候选解析。

``CapabilityResolver`` 是**唯一候选生成真源**（ARCHITECTURE §5.4）：所有 active
路由与 shadow 观测都必须消费这同一份 ``CandidateSet``，shadow 不得再算一次。

解析只看三样东西：``IntentDraft`` 的意图、``RequestContext``、以及传入的
``CapabilitySnapshot``。**它不看模型的置信度，也不接受槽位里的能力标识**——否则
模型就能直接选中一个 capability，而这正是 ADR-007 D7 要排除的。
"""

from typing import Final

from xiaowei_agent.contracts import (
    Candidate,
    CandidateSet,
    CapabilitySnapshot,
    IntentDraft,
    Rejection,
    RequestContext,
)

RESOLVER_VERSION: Final[str] = "resolver.deterministic.v1"

_EXACT_MATCH_SCORE: Final[float] = 1.0

_MATCH_EVIDENCE: Final[str] = "intent equals the declared capability id"
_NO_MATCH_REASON: Final[str] = "intent.does_not_match_capability"

_REQUIRED_CONTEXT: Final[tuple[str, ...]] = ("tenant_id", "environment_id")


class DeterministicCapabilityResolver:
    """按"意图是否等于已声明能力标识"逐条判定的解析器。

    判定刻意做成**相等比较**而不是模糊匹配：模糊匹配的阈值会成为一个可以悄悄
    调松的旋钮，而"是否命中"必须有确定答案。意图字符串本身来自解释器的闭集，
    不是模型自由文本。
    """

    def resolve(
        self,
        *,
        draft: IntentDraft,
        context: RequestContext,
        snapshot: CapabilitySnapshot,
    ) -> CandidateSet:
        """产出本次请求的唯一候选集合。

        每个 ``(capability_id, version, operation)`` 三元组要么是候选、要么被拒绝，
        不会两者皆是——``CandidateSet`` 的校验器承重这一点。
        """
        # context 参与签名但当前不参与判定：M3 只有一个能力，没有按租户/环境
        # 分区的声明。保留参数是为了让"解析看得到上下文"成为契约的一部分，
        # 而不是等到需要时再改签名。
        _ = context
        items: list[Candidate] = []
        rejections: list[Rejection] = []
        for spec in snapshot.specs:
            matched = draft.intent == spec.capability_id
            for operation in spec.operations:
                if matched:
                    items.append(
                        Candidate(
                            capability_id=spec.capability_id,
                            capability_version=spec.version,
                            operation=operation.operation,
                            score=_EXACT_MATCH_SCORE,
                            match_evidence=(_MATCH_EVIDENCE,),
                            required_context=_REQUIRED_CONTEXT,
                        )
                    )
                else:
                    rejections.append(
                        Rejection(
                            capability_id=spec.capability_id,
                            capability_version=spec.version,
                            operation=operation.operation,
                            reason_code=_NO_MATCH_REASON,
                        )
                    )
        return CandidateSet(
            resolver_version=RESOLVER_VERSION,
            snapshot_id=snapshot.snapshot_id,
            items=tuple(items),
            rejections=tuple(rejections),
        )

"""审批绑定与 policy revision 的确定性校验。

M2 只交付**纯校验函数**，不实现 ApprovalGate（属 M3）。独立成纯函数的好处是：
它在 M2 就能被完整验收，且 M3 的 ApprovalGate 与 M4 的恢复路径调用的是同一份
实现，不会各写一套。

**校验顺序即拒绝原因的优先级**：先看审批本身是否有效（过期 → 未授予），再看它绑定
的三个指纹是否漂移。顺序固定后，审计日志里的拒绝原因才指向最具体的违规，而不是被
一个更宽泛的原因掩盖。
"""

import datetime as _dt

from xiaowei_agent.contracts import (
    ApprovalRequest,
    ApprovalState,
    BindingRejection,
    ExecutionPlan,
    PolicySnapshot,
    ResolvedTarget,
)
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint


class BindingError(RuntimeError):
    """审批绑定或 policy revision 校验失败。``rejection`` 给出最具体的原因。"""

    def __init__(self, rejection: BindingRejection, detail: str) -> None:
        super().__init__(f"{rejection.value}: {detail}")
        self.rejection = rejection


def verify_policy_revision(snapshot: PolicySnapshot, *, plan: ExecutionPlan) -> None:
    """计划依据的 policy revision 与 profile 必须是当前生效的。

    :raises BindingError: revision 不匹配，或 profile 未在当前快照注册。
    """
    if plan.policy_revision != snapshot.policy_revision:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "plan policy_revision is not the active revision",
        )
    if plan.policy_profile not in snapshot.profiles:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "plan policy_profile is not registered in the active snapshot",
        )


def verify_approval_binding(
    *,
    approval: ApprovalRequest,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    now: _dt.datetime,
) -> None:
    """恢复时重算并核对审批绑定（ARCHITECTURE §5.7）。

    :raises BindingError: 审批过期/未授予，或计划、目标、policy revision 漂移。
    """
    # ``approval.expires_at`` 由 AwareDatetime 保证带时区；``now`` 是普通参数，
    # naive 值与 aware 值相比较会抛 TypeError——那是崩溃，不是 fail-closed 的拒绝。
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise BindingError(
            BindingRejection.APPROVAL_EXPIRED, "now must be timezone-aware"
        )
    if approval.expires_at <= now:
        raise BindingError(BindingRejection.APPROVAL_EXPIRED, "approval has expired")
    if approval.state is not ApprovalState.GRANTED:
        raise BindingError(
            BindingRejection.APPROVAL_NOT_GRANTED,
            f"approval state is {approval.state.value}",
        )
    if approval.policy_revision != plan.policy_revision:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "approval was granted under a different policy revision",
        )
    if approval.plan_hash != compute_plan_hash(plan):
        raise BindingError(BindingRejection.PLAN_DRIFT, "plan_hash mismatch")
    if approval.target_fingerprint != compute_target_fingerprint(target):
        raise BindingError(BindingRejection.TARGET_DRIFT, "target_fingerprint mismatch")

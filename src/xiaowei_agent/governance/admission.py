"""准入凭证的唯一签发入口。

``AdmissionCertificate`` 自身拒绝普通构造（签发凭据模式，与 ``ToolResult`` 相同），
真正的签发只能经过这里。M2 没有 ToolPolicy / SQLGuard / ApprovalGate（属 M3），
因此本函数不做策略判定：它接收上游已经算好的 ``PolicyDecision``，只负责

1. 把凭证与**调用内容**绑定——``tool_call_hash`` 由本函数从 ``ToolCall`` 重算，
   不接受调用方传入。否则"绑定调用内容"这件事又回到调用方的诚实上。
2. 把凭证与**当前 policy revision** 绑定——``PolicyDecision.policy_revision``
   必须等于本次请求上下文的 revision，否则签发即失败。

M3 接入真实 StepAdmission 时，策略判定在本函数之前完成，签名不变。
"""

from xiaowei_agent.contracts import (
    AdmissionCertificate,
    EffectClass,
    PolicyDecision,
    RequestContext,
    Sha256Hex,
    StrictStr,
    ToolCall,
)
from xiaowei_agent.contracts.approval import _ADMISSION_WITNESS, _ADMISSION_WITNESS_KEY
from xiaowei_agent.contracts.enums import BindingRejection
from xiaowei_agent.governance.binding import BindingError
from xiaowei_agent.planning import compute_tool_call_hash


def issue_admission_certificate(
    *,
    call: ToolCall,
    context: RequestContext,
    decision: PolicyDecision,
    effect_class: EffectClass,
    plan_hash: Sha256Hex,
    target_fingerprint: Sha256Hex,
    approval_ref: StrictStr | None = None,
) -> AdmissionCertificate:
    """签发一张与本次调用和当前 revision 绑定的准入凭证。

    :raises BindingError: 决策的 policy_revision 不是本次上下文的当前 revision。
    """
    if decision.policy_revision != context.policy_revision:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "policy decision was made under a different policy revision",
        )
    return AdmissionCertificate.model_validate(
        {
            _ADMISSION_WITNESS_KEY: _ADMISSION_WITNESS,
            "step_id": call.step_id,
            "operation": call.operation,
            "effect_class": effect_class,
            "policy_decision": decision,
            "approval_ref": approval_ref,
            "plan_hash": plan_hash,
            "target_fingerprint": target_fingerprint,
            # 从 call 重算，不接受传入：否则绑定调用内容这件事回到调用方的诚实上。
            "tool_call_hash": compute_tool_call_hash(call),
        }
    )

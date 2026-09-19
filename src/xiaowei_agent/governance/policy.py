"""ToolPolicy：确定性的工具调用策略判定。

判定只看**已声明的允许面**与本次调用的结构化事实（operation、分类、环境、租户、
超时），不看模型输出、不看外部文本、不看 adapter 返回值。

**fail-closed 的形状**：函数返回一个 ``PolicyDecision``，任何不满足的条件都产生
``allow=False`` 与一个闭集拒绝原因；调用方（``StepAdmission``）据此中止。这样
"为什么被拒"在审计里是可聚合的枚举成员，而不是一句自由文本。
"""

from xiaowei_agent.contracts import (
    EffectClass,
    ExecutionPlan,
    PolicyDecision,
    PolicyProfile,
    PolicyReason,
    ReadClass,
    RequestContext,
    ResolvedTarget,
    ToolCall,
)


class PolicyDeniedError(RuntimeError):
    """策略拒绝。``reason`` 与 ``decision`` 给出结构化理由。

    消息里只出现闭集枚举成员：被拒的 operation、环境与超时都来自调用方。

    **两个参数都是关键字**：``BaseException.__str__`` 渲染的是 ``args``，而
    ``args`` 只收位置参数。写成关键字使判定对象在结构上进不了错误文本，而不是
    依赖"我们记得不要把它拼进去"。
    """

    def __init__(self, *, decision: PolicyDecision, reason: PolicyReason) -> None:
        super().__init__(reason.value)
        self.decision = decision
        self.reason = reason


def _decide(profile: PolicyProfile, reason: PolicyReason, revision: str) -> PolicyDecision:
    return PolicyDecision(
        allow=reason is PolicyReason.ALLOWED,
        reason_code=reason.value,
        risk=profile.risk,
        policy_revision=revision,
        obligations=(),
    )


def evaluate_tool_policy(
    *,
    profile: PolicyProfile,
    plan: ExecutionPlan,
    call: ToolCall,
    context: RequestContext,
    target: ResolvedTarget,
    effect_class: EffectClass,
    read_class: ReadClass | None,
) -> PolicyDecision:
    """判定一次工具调用是否被当前 profile 允许。

    :param effect_class: **已由快照派生**的分类，不接受调用方自称的取值——它由
        ``StepAdmission`` 在上一段用 ``verify_plan_effects`` 重算后传入。
    :returns: 允许或拒绝的结构化判定；判定本身不抛异常。
    """
    revision = context.policy_revision
    if plan.policy_profile != profile.profile_id:
        return _decide(profile, PolicyReason.PROFILE_MISMATCH, revision)
    if target.tenant_id != context.tenant_id:
        return _decide(profile, PolicyReason.TENANT_MISMATCH, revision)
    if target.environment_id != context.environment_id:
        return _decide(profile, PolicyReason.ENVIRONMENT_MISMATCH, revision)
    if call.operation not in profile.allowed_operations:
        return _decide(profile, PolicyReason.OPERATION_NOT_ALLOWED, revision)
    if effect_class not in profile.allowed_effect_classes:
        return _decide(profile, PolicyReason.EFFECT_CLASS_NOT_ALLOWED, revision)
    if effect_class is EffectClass.READ:
        if read_class not in profile.allowed_read_classes:
            return _decide(profile, PolicyReason.READ_CLASS_NOT_ALLOWED, revision)
    elif read_class is not None:
        return _decide(profile, PolicyReason.READ_CLASS_NOT_ALLOWED, revision)
    if context.environment_id not in profile.allowed_environment_ids:
        return _decide(profile, PolicyReason.ENVIRONMENT_NOT_ALLOWED, revision)
    if call.timeout_seconds > profile.max_timeout_seconds:
        return _decide(profile, PolicyReason.TIMEOUT_EXCEEDS_PROFILE, revision)
    return _decide(profile, PolicyReason.ALLOWED, revision)

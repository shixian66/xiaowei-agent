"""StepAdmission：六段准入的**唯一**入口。

```text
1 verify_policy_revision     计划依据的 revision 与 profile 是否当前生效
2 verify_plan_effects        分类从快照重算，与步骤标记双向比对
3 ToolPolicy.evaluate        允许面判定
4 SQLGuard.verify_sql        携带 SQL 的步骤才做
5 ApprovalGate.require       副作用步骤才做
6 issue_admission_certificate
```

**顺序即拒绝原因的优先级。** 顺序错了，审计里看到的会是更宽泛的原因：例如策略
判定排在分类重算之前时，一个被伪标成只读的写步骤会以"operation 不在允许面内"
被拒——理由正确，但掩盖了"计划被篡改"这个真正的事实。

**分类必须在准入边界重算**（第 2 段），不能依赖 Runner 记得调用：计划会经存储
往返、进程重启与并发抢占，只有重算才能覆盖持久化之后被篡改的情形。
"""

import datetime as _dt

from xiaowei_agent.capabilities.effect import derive_effect, verify_plan_effects
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    ApprovalRequest,
    ExecutionPlan,
    PlanStep,
    PolicyProfile,
    PolicyReason,
    PolicySnapshot,
    RequestContext,
    ResolvedTarget,
    SqlSurface,
    ToolCall,
)
from xiaowei_agent.contracts.capability import CapabilitySnapshot
from xiaowei_agent.contracts.enums import SqlGuardRejection
from xiaowei_agent.governance.admission import issue_admission_certificate
from xiaowei_agent.governance.approval import ApprovalGate
from xiaowei_agent.governance.binding import verify_policy_revision
from xiaowei_agent.governance.policy import PolicyDeniedError, evaluate_tool_policy
from xiaowei_agent.governance.sqlguard import SqlGuardError, verify_sql
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

_SQL_KEY = "sql"
_TEMPLATE_KEY = "sql_template_id"


def _verify_step_sql(
    *, step: PlanStep, surface: SqlSurface
) -> None:
    """第 4 段：携带 SQL 的步骤必须过 SQLGuard。

    **准入不信任步骤自称的 SQL**：参数从 ``typed_arguments`` 经
    ``from_typed_arguments`` 重新校验后还原，SQLGuard 再用它重编译比对。少了这一步，
    比对就会基于一份未经校验的参数进行，比对本身失去意义。

    两个信封键必须同时存在或同时不存在：只有一个时，"这条 SQL 由哪个模板产生"没有
    答案，无法比对——fail-closed。
    """
    sql = step.typed_arguments.get(_SQL_KEY)
    template_id = step.typed_arguments.get(_TEMPLATE_KEY)
    if sql is None and template_id is None:
        return
    if not isinstance(sql, str) or not isinstance(template_id, str):
        raise SqlGuardError(SqlGuardRejection.UNKNOWN_TEMPLATE)
    params = SlowQueryParams.from_typed_arguments(step.typed_arguments)
    verify_sql(sql=sql, params=params, surface=surface, template_id=template_id)


def admit_step(
    *,
    step: PlanStep,
    plan: ExecutionPlan,
    call: ToolCall,
    context: RequestContext,
    target: ResolvedTarget,
    snapshot: CapabilitySnapshot,
    policy_snapshot: PolicySnapshot,
    profile: PolicyProfile,
    surface: SqlSurface,
    approval_gate: ApprovalGate,
    task_id: str,
    now: _dt.datetime,
    approval: ApprovalRequest | None = None,
) -> AdmissionCertificate:
    """对一个步骤执行六段准入，通过则签发准入凭证。

    :returns: 与**本次调用内容**绑定的凭证；``ToolGateway`` 消费它。
    :raises BindingError: policy revision 漂移，或审批绑定不成立。
    :raises SpecResolutionError: 分类无法派生，或与步骤标记冲突。
    :raises PolicyDeniedError: 允许面判定拒绝。
    :raises SqlGuardError: SQL 未通过 AST 校验或重编译比对。
    :raises ApprovalRequiredError: 副作用步骤缺少有效审批。
    """
    verify_policy_revision(policy_snapshot, plan=plan)
    verify_plan_effects(snapshot, plan)
    declared = derive_effect(
        snapshot,
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        operation=step.operation,
    )
    decision = evaluate_tool_policy(
        profile=profile,
        plan=plan,
        call=call,
        context=context,
        target=target,
        effect_class=declared.effect_class,
    )
    if not decision.allow:
        raise PolicyDeniedError(
            decision=decision, reason=PolicyReason(decision.reason_code)
        )
    _verify_step_sql(step=step, surface=surface)
    approval_ref: str | None = None
    if declared.side_effect:
        # 只有副作用步骤才走审批：ApprovalGate 是执行中断点，不是入口总开关。
        approval_ref = approval_gate.require(
            task_id=task_id,
            step=step,
            plan=plan,
            target=target,
            approval=approval,
            now=now,
        )
    return issue_admission_certificate(
        call=call,
        context=context,
        decision=decision,
        effect_class=declared.effect_class,
        plan_hash=compute_plan_hash(plan),
        target_fingerprint=compute_target_fingerprint(target),
        approval_ref=approval_ref,
    )

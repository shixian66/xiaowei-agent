"""StepAdmission：六段准入的**唯一**入口。

```text
1 verify_policy_revision     计划依据的 revision 与 profile 是否当前生效
2 verify_plan_declaration    分类从快照重算，并核对步骤标记与 ToolCall gateway
3 ToolPolicy.evaluate        允许面判定
4 QueryGuard                 携带 SQL/PromQL 的步骤走对应重编译闸门
5 ApprovalGate.require       副作用步骤才做
6 issue_admission_certificate
```

**顺序即拒绝原因的优先级。** 顺序错了，审计里看到的会是更宽泛的原因：例如策略
判定排在分类重算之前时，一个被伪标成只读的写步骤会以"operation 不在允许面内"
被拒——理由正确，但掩盖了"计划被篡改"这个真正的事实。

**operation 声明必须在准入边界重算**（第 2 段），不能依赖 Runner 记得调用：计划会
经存储往返、进程重启与并发抢占，只有重算才能覆盖持久化之后被篡改的分类或错误
gateway。
"""

import datetime as _dt

from xiaowei_agent.capabilities.effect import (
    SpecResolutionError,
    derive_effect,
    verify_plan_effects,
)
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    ApprovalRequest,
    ExecutionPlan,
    PlanStep,
    PolicyProfile,
    PolicyReason,
    PolicySnapshot,
    PromqlGuardRejection,
    PromqlSurface,
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
from xiaowei_agent.governance.promqlguard import PromqlGuardError, verify_promql
from xiaowei_agent.governance.sqlguard import SqlGuardError, verify_sql
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

_SQL_KEY = "sql"
_SQL_TEMPLATE_KEY = "sql_template_id"
_PROMQL_KEY = "promql"
_PROMQL_TEMPLATE_KEY = "promql_template_id"


def _verify_step_query(
    *,
    step: PlanStep,
    sql_surface: SqlSurface | None,
    promql_surface: PromqlSurface | None,
) -> None:
    """第 4 段：查询信封必须完整、互斥，并通过对应闸门。

    **准入不信任步骤自称的 SQL**：参数从 ``typed_arguments`` 经
    ``from_typed_arguments`` 重新校验后还原，SQLGuard 再用它重编译比对。少了这一步，
    比对就会基于一份未经校验的参数进行，比对本身失去意义。

    同类信封键必须同时存在或同时不存在，SQL 与 PromQL 也不能同时出现。无查询
    信封的普通只读步骤继续由计划哈希、ToolPolicy 和 adapter 操作闭集约束。
    """
    sql_keys = (
        _SQL_KEY in step.typed_arguments,
        _SQL_TEMPLATE_KEY in step.typed_arguments,
    )
    promql_keys = (
        _PROMQL_KEY in step.typed_arguments,
        _PROMQL_TEMPLATE_KEY in step.typed_arguments,
    )
    if any(sql_keys) and not all(sql_keys):
        raise SqlGuardError(SqlGuardRejection.UNKNOWN_TEMPLATE)
    if any(promql_keys) and not all(promql_keys):
        raise PromqlGuardError(PromqlGuardRejection.INCOMPLETE_ENVELOPE)
    if all(sql_keys) and all(promql_keys):
        raise PromqlGuardError(PromqlGuardRejection.ENVELOPE_CONFLICT)
    if not any(sql_keys) and not any(promql_keys):
        return
    if all(sql_keys):
        sql = step.typed_arguments[_SQL_KEY]
        template_id = step.typed_arguments[_SQL_TEMPLATE_KEY]
        if not isinstance(sql, str) or not isinstance(template_id, str):
            raise SqlGuardError(SqlGuardRejection.UNKNOWN_TEMPLATE)
        if sql_surface is None:
            raise SqlGuardError(SqlGuardRejection.UNKNOWN_TEMPLATE)
        params = SlowQueryParams.from_typed_arguments(step.typed_arguments)
        verify_sql(
            sql=sql,
            params=params,
            surface=sql_surface,
            template_id=template_id,
        )
        return
    promql = step.typed_arguments[_PROMQL_KEY]
    template_id = step.typed_arguments[_PROMQL_TEMPLATE_KEY]
    if not isinstance(promql, str) or not isinstance(template_id, str):
        raise PromqlGuardError(PromqlGuardRejection.INVALID_ARGUMENTS)
    if promql_surface is None:
        raise PromqlGuardError(PromqlGuardRejection.SURFACE_MISSING)
    verify_promql(
        promql=promql,
        template_id=template_id,
        typed_arguments=step.typed_arguments,
        surface=promql_surface,
    )


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
    sql_surface: SqlSurface | None,
    promql_surface: PromqlSurface | None,
    approval_gate: ApprovalGate,
    task_id: str,
    now: _dt.datetime,
    approval: ApprovalRequest | None = None,
) -> AdmissionCertificate:
    """对一个步骤执行六段准入，通过则签发准入凭证。

    :returns: 与**本次调用内容**绑定的凭证；``ToolGateway`` 消费它。
    :raises BindingError: policy revision 漂移，或审批绑定不成立。
    :raises SpecResolutionError: 声明无法派生，或分类/gateway 与调用冲突。
    :raises PolicyDeniedError: 允许面判定拒绝。
    :raises SqlGuardError: SQL 未通过 AST 校验或重编译比对。
    :raises PromqlGuardError: PromQL 信封或重编译比对不成立。
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
    if call.gateway != declared.gateway:
        raise SpecResolutionError("call gateway differs from operation declaration")
    decision = evaluate_tool_policy(
        profile=profile,
        plan=plan,
        call=call,
        context=context,
        target=target,
        effect_class=declared.effect_class,
        read_class=declared.read_class,
    )
    if not decision.allow:
        raise PolicyDeniedError(
            decision=decision, reason=PolicyReason(decision.reason_code)
        )
    _verify_step_query(
        step=step,
        sql_surface=sql_surface,
        promql_surface=promql_surface,
    )
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

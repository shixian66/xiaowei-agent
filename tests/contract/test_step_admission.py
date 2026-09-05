"""StepAdmission：六段准入的唯一入口。

顺序即拒绝原因的优先级：``verify_policy_revision`` → ``verify_plan_effects`` →
``ToolPolicy`` → ``SQLGuard`` → ``ApprovalGate`` → 签发凭证。顺序错了，审计里
看到的会是更宽泛的原因，错误归因随之失真。
"""

import datetime as dt

import pytest
from tests.fakes.admission import (
    NOW,
    admit,
    forged_write_step,
    granted_approval,
    slow_query_call,
    slow_query_plan,
    slow_query_step,
    synthetic_write_call,
    synthetic_write_plan,
    synthetic_write_step,
)

from xiaowei_agent.capabilities.effect import SpecResolutionError
from xiaowei_agent.contracts import (
    EffectClass,
    PlanStep,
    PolicyReason,
    PromqlGuardRejection,
    PromqlSurface,
)
from xiaowei_agent.governance.approval import ApprovalRequiredError
from xiaowei_agent.governance.binding import BindingError
from xiaowei_agent.governance.policy import PolicyDeniedError
from xiaowei_agent.governance.promqlguard import PromqlGuardError
from xiaowei_agent.governance.sqlguard import SqlGuardError
from xiaowei_agent.planning.prometheus.compiler import compile_promql
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.templates import (
    CPU_PERCENT_V1,
    INSTANCE_UP_V1,
)

PROMQL_SURFACE = PromqlSurface(
    surface_id="promql.prometheus.alert.evidence.v1",
    allowed_template_ids=(CPU_PERCENT_V1, INSTANCE_UP_V1),
    max_window_minutes=360,
    max_series=5,
    max_points_per_series=361,
)


def _promql_step(**envelope: object) -> PlanStep:
    params = PrometheusAlertParams(
        alert_name="HostHighCpu",
        instance="node-1.example.com:9100",
        window_start=NOW - dt.timedelta(minutes=30),
        window_end=NOW,
    )
    arguments = params.to_typed_arguments() | {
        "promql": compile_promql(
            template_id=CPU_PERCENT_V1, params=params, surface=PROMQL_SURFACE
        ),
        "promql_template_id": CPU_PERCENT_V1,
    }
    arguments.update(envelope)
    return slow_query_step().model_copy(update={"typed_arguments": arguments})


def test_a_read_step_is_admitted_and_bound_to_this_call() -> None:
    """先证明准入不是恒拒。"""
    call = slow_query_call()
    certificate = admit(step=slow_query_step(), call=call)
    assert certificate.step_id == call.step_id
    assert certificate.operation == call.operation
    assert certificate.effect_class is EffectClass.READ
    assert certificate.policy_decision.allow is True
    assert certificate.approval_ref is None


def test_admission_binds_the_current_plan_and_target_fingerprints() -> None:
    from tests.fakes.admission import TARGET

    from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint

    certificate = admit(step=slow_query_step(), call=slow_query_call())
    assert certificate.plan_hash == compute_plan_hash(slow_query_plan())
    assert certificate.target_fingerprint == compute_target_fingerprint(TARGET)


def test_stale_policy_revision_is_refused_first() -> None:
    """第 1 段：policy revision 漂移优先于其余一切。"""
    stale = slow_query_plan().model_copy(update={"policy_revision": "policy-2020-01-01"})
    with pytest.raises(BindingError):
        admit(step=slow_query_step(), call=slow_query_call(), plan=stale)


def test_mislabelled_write_step_is_refused_before_policy_runs() -> None:
    """第 2 段：伪标拒绝先于 ToolPolicy。

    直接构造 PlanStep 只在测试里可行（src 内由 test_effect_single_source 禁止），
    这正是要模拟的"计划在存储里被篡改"。
    """
    forged = forged_write_step()
    with pytest.raises(SpecResolutionError):
        admit(
            step=forged,
            call=synthetic_write_call(),
            plan=synthetic_write_plan(steps=(forged,)),
        )


def test_operation_outside_the_profile_is_denied() -> None:
    """第 3 段：ToolPolicy。"""
    with pytest.raises(PolicyDeniedError) as err:
        admit(
            step=slow_query_step(),
            call=slow_query_call(operation="drop_everything"),
        )
    assert err.value.reason is PolicyReason.OPERATION_NOT_ALLOWED


def test_environment_outside_the_profile_is_denied() -> None:
    from tests.fakes.admission import CONTEXT, TARGET

    other = CONTEXT.model_copy(update={"environment_id": "test"})
    with pytest.raises(PolicyDeniedError) as err:
        admit(
            step=slow_query_step(),
            call=slow_query_call(),
            context=other,
            target=TARGET.model_copy(update={"environment_id": "test"}),
            profile_environments=("dev",),
        )
    assert err.value.reason is PolicyReason.ENVIRONMENT_NOT_ALLOWED


def test_target_environment_must_match_the_request_context() -> None:
    """允许目录不能掩盖目标与本次请求环境不一致。"""
    from tests.fakes.admission import TARGET

    drifted = TARGET.model_copy(update={"environment_id": "test"})
    with pytest.raises(PolicyDeniedError) as err:
        admit(
            step=slow_query_step(),
            call=slow_query_call(),
            target=drifted,
            profile_environments=("dev", "test"),
        )
    assert err.value.reason is PolicyReason.ENVIRONMENT_MISMATCH


def test_timeout_beyond_the_profile_cap_is_denied() -> None:
    with pytest.raises(PolicyDeniedError) as err:
        admit(step=slow_query_step(), call=slow_query_call(timeout_seconds=299.0))
    assert err.value.reason is PolicyReason.TIMEOUT_EXCEEDS_PROFILE


def test_tampered_sql_is_refused_by_the_guard() -> None:
    """第 4 段：SQLGuard。准入不信任步骤自称的 SQL。"""
    step = slow_query_step()
    tampered = dict(step.typed_arguments)
    tampered["sql"] = tampered["sql"].replace("LIMIT 20", "LIMIT 200")
    hostile = step.model_copy(update={"typed_arguments": tampered})
    with pytest.raises(SqlGuardError):
        admit(step=hostile, call=slow_query_call(), plan=slow_query_plan(steps=(hostile,)))


def test_sql_envelope_without_a_registered_surface_is_refused() -> None:
    with pytest.raises(SqlGuardError):
        admit(step=slow_query_step(), call=slow_query_call(), sql_surface=None)


def test_registered_promql_envelope_is_admitted() -> None:
    step = _promql_step()
    call = slow_query_call(typed_args=dict(step.typed_arguments))
    certificate = admit(
        step=step,
        plan=slow_query_plan(steps=(step,)),
        call=call,
        sql_surface=None,
        promql_surface=PROMQL_SURFACE,
    )
    assert certificate.step_id == step.step_id


@pytest.mark.parametrize("gateway", ["prometheus", "unregistered"])
def test_call_gateway_must_match_the_declared_operation(gateway: str) -> None:
    step = slow_query_step()
    with pytest.raises(SpecResolutionError, match="gateway"):
        admit(
            step=step,
            plan=slow_query_plan(steps=(step,)),
            call=slow_query_call(gateway=gateway),
        )


def test_promql_envelope_call_cannot_be_redirected_to_alertmanager() -> None:
    step = _promql_step()
    call = slow_query_call(
        gateway="alertmanager", typed_args=dict(step.typed_arguments)
    )
    with pytest.raises(SpecResolutionError, match="gateway"):
        admit(
            step=step,
            plan=slow_query_plan(steps=(step,)),
            call=call,
            sql_surface=None,
            promql_surface=PROMQL_SURFACE,
        )


@pytest.mark.parametrize(
    "typed_arguments",
    [
        {"sql": "SELECT 1"},
        {"sql_template_id": "starrocks.slow_query.list.v1"},
        {"promql": "up"},
        {"promql_template_id": INSTANCE_UP_V1},
    ],
)
def test_partial_query_envelope_is_rejected(
    typed_arguments: dict[str, object],
) -> None:
    step = slow_query_step().model_copy(update={"typed_arguments": typed_arguments})
    with pytest.raises((SqlGuardError, PromqlGuardError)):
        admit(
            step=step,
            plan=slow_query_plan(steps=(step,)),
            call=slow_query_call(typed_args=typed_arguments),
            promql_surface=PROMQL_SURFACE,
        )


def test_sql_and_promql_envelopes_cannot_coexist() -> None:
    sql_step = slow_query_step()
    promql_step = _promql_step(**dict(sql_step.typed_arguments))
    with pytest.raises(PromqlGuardError) as caught:
        admit(
            step=promql_step,
            plan=slow_query_plan(steps=(promql_step,)),
            call=slow_query_call(typed_args=dict(promql_step.typed_arguments)),
            promql_surface=PROMQL_SURFACE,
        )
    assert caught.value.rejection is PromqlGuardRejection.ENVELOPE_CONFLICT


def test_promql_envelope_without_a_registered_surface_is_refused() -> None:
    step = _promql_step()
    with pytest.raises(PromqlGuardError) as caught:
        admit(
            step=step,
            plan=slow_query_plan(steps=(step,)),
            call=slow_query_call(typed_args=dict(step.typed_arguments)),
            sql_surface=None,
        )
    assert caught.value.rejection is PromqlGuardRejection.SURFACE_MISSING


def test_admission_reparses_params_from_typed_arguments() -> None:
    """篡改参数而不动 SQL，同样必须被重编译比对挡下。"""
    step = slow_query_step()
    tampered = dict(step.typed_arguments)
    tampered["row_limit"] = 7
    hostile = step.model_copy(update={"typed_arguments": tampered})
    with pytest.raises(SqlGuardError):
        admit(step=hostile, call=slow_query_call(), plan=slow_query_plan(steps=(hostile,)))


def test_side_effect_step_without_approval_is_refused() -> None:
    """第 5 段：ApprovalGate。"""
    with pytest.raises(ApprovalRequiredError):
        admit(
            step=synthetic_write_step(),
            call=synthetic_write_call(),
            plan=synthetic_write_plan(),
            approval=None,
        )


def test_read_step_never_consults_the_approval_gate() -> None:
    """只读步骤不触发审批：ApprovalGate 是副作用步骤的中断点，不是入口总开关。"""
    from tests.fakes.admission import CountingApprovalGate

    gate = CountingApprovalGate()
    admit(step=slow_query_step(), call=slow_query_call(), approval_gate=gate)
    assert gate.calls == 0


def test_granted_approval_yields_a_certificate_carrying_its_reference() -> None:
    approval = granted_approval()
    certificate = admit(
        step=synthetic_write_step(),
        call=synthetic_write_call(),
        plan=synthetic_write_plan(),
        approval=approval,
    )
    assert certificate.approval_ref is not None
    assert certificate.effect_class is EffectClass.MUTATE_TARGET


def test_expired_approval_is_refused() -> None:
    approval = granted_approval(expires_at=NOW - dt.timedelta(seconds=1))
    with pytest.raises(BindingError):
        admit(
            step=synthetic_write_step(),
            call=synthetic_write_call(),
            plan=synthetic_write_plan(),
            approval=approval,
        )


def test_approval_for_another_step_is_refused() -> None:
    approval = granted_approval(step_id="s99")
    with pytest.raises(BindingError):
        admit(
            step=synthetic_write_step(),
            call=synthetic_write_call(),
            plan=synthetic_write_plan(),
            approval=approval,
        )


def test_certificate_is_bound_to_the_exact_call() -> None:
    """凭证证明的是"这一个 ToolCall 已准入"，不是"这个步骤已准入"。"""
    from xiaowei_agent.planning import compute_tool_call_hash

    call = slow_query_call()
    certificate = admit(step=slow_query_step(), call=call)
    assert certificate.tool_call_hash == compute_tool_call_hash(call)
    other = call.model_copy(update={"idempotency_key": "idem-2"})
    assert certificate.tool_call_hash != compute_tool_call_hash(other)

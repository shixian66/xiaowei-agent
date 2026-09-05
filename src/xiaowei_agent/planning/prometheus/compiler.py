"""从闭集参数与模板生成确定性的 PromQL。"""

import datetime as dt
from typing import Final

from xiaowei_agent.capabilities.effect import build_plan_step
from xiaowei_agent.capabilities.prometheus_alert import (
    OP_GET_ACTIVE_ALERTS,
    OP_QUERY_METRIC_RANGE,
)
from xiaowei_agent.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionPlan,
    JsonScalar,
    PlanBudget,
    PlanStep,
    PromqlSurface,
    RequestContext,
    ResolvedTarget,
    StepCondition,
    StepConditionKind,
    StepResultStatus,
)
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.templates import (
    CPU_PERCENT_V1,
    INSTANCE_UP_V1,
    escape_promql_label_value,
    template_for_alert,
)


def compile_promql(
    *, template_id: str, params: PrometheusAlertParams, surface: PromqlSurface
) -> str:
    """编译一个已注册模板，并同时执行声明预算检查。"""
    if template_id not in surface.allowed_template_ids:
        raise ValueError("template is outside the declared surface")
    if template_for_alert(params.alert_name) != template_id:
        raise ValueError("template does not match alert_name")
    window = params.window_end - params.window_start
    points = int(window.total_seconds()) // params.step_seconds + 1
    if window > dt.timedelta(minutes=surface.max_window_minutes):
        raise ValueError("window exceeds the declared surface")
    if params.max_series > surface.max_series:
        raise ValueError("series budget exceeds the declared surface")
    if points > surface.max_points_per_series:
        raise ValueError("point budget exceeds the declared surface")
    instance = escape_promql_label_value(params.instance)
    if template_id == CPU_PERCENT_V1:
        return (
            "100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{"
            f'mode="idle",instance="{instance}"}}[5m])))'
        )
    if template_id == INSTANCE_UP_V1:
        return f'up{{instance="{instance}"}}'
    raise ValueError("template is not implemented")


ALERT_LIMIT: Final[int] = 5
STEP_ALERTS: Final[str] = "s1"
STEP_METRIC: Final[str] = "s2"
ALERT_PLAN_BUDGET: Final[PlanBudget] = PlanBudget(
    max_steps=2, max_tool_calls=2, max_model_tokens=4000
)
ALERT_ENTRY_OPERATION: Final[str] = OP_GET_ACTIVE_ALERTS


def _alert_arguments(params: PrometheusAlertParams) -> dict[str, JsonScalar]:
    arguments: dict[str, JsonScalar] = {
        "alert_name": params.alert_name,
        "instance": params.instance,
        "limit": ALERT_LIMIT,
    }
    if params.fingerprint is not None:
        arguments["fingerprint"] = params.fingerprint
    return arguments


def _metric_arguments(
    *, params: PrometheusAlertParams, surface: PromqlSurface
) -> dict[str, JsonScalar]:
    template_id = template_for_alert(params.alert_name)
    arguments = params.to_typed_arguments()
    arguments.pop("fingerprint")
    return {
        "promql": compile_promql(
            template_id=template_id, params=params, surface=surface
        ),
        "promql_template_id": template_id,
        **arguments,
    }


def compile_alert_plan(
    *,
    candidate: Candidate,
    params: PrometheusAlertParams,
    target: ResolvedTarget,
    context: RequestContext,
    snapshot: CapabilitySnapshot,
    surface: PromqlSurface,
) -> ExecutionPlan:
    """编译固定的 Alertmanager → Prometheus 两步只读计划。"""
    if candidate.operation != ALERT_ENTRY_OPERATION:
        raise ValueError("candidate does not point at the plan entry operation")
    if target.environment_id != context.environment_id:
        raise ValueError("target environment does not match the request context")
    spec = next(
        (
            item
            for item in snapshot.specs
            if item.capability_id == candidate.capability_id
            and item.version == candidate.capability_version
        ),
        None,
    )
    if spec is None:
        raise ValueError("candidate capability is absent from the snapshot")
    steps: tuple[PlanStep, ...] = (
        build_plan_step(
            snapshot,
            capability_id=candidate.capability_id,
            capability_version=candidate.capability_version,
            operation=OP_GET_ACTIVE_ALERTS,
            step_id=STEP_ALERTS,
            typed_arguments=_alert_arguments(params),
        ),
        build_plan_step(
            snapshot,
            capability_id=candidate.capability_id,
            capability_version=candidate.capability_version,
            operation=OP_QUERY_METRIC_RANGE,
            step_id=STEP_METRIC,
            typed_arguments=_metric_arguments(params=params, surface=surface),
            depends_on=(STEP_ALERTS,),
            condition=StepCondition(
                kind=StepConditionKind.PRIOR_STEP_RESULT_IS,
                ref_step_id=STEP_ALERTS,
                expected_result=StepResultStatus.OK,
            ),
        ),
    )
    return ExecutionPlan(
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        steps=steps,
        policy_profile=spec.policy_profile,
        policy_revision=context.policy_revision,
        budget=ALERT_PLAN_BUDGET,
    )

"""生产 capability bindings 的显式装配；不做动态发现。"""

import datetime as dt
from dataclasses import dataclass, replace
from typing import Final

from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingRegistry,
    CapabilityPreparationError,
    CapabilityRuntimeBinding,
    PreparedCapability,
)
from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
    ASSET_INVENTORY_CAPABILITY_VERSION,
    OP_LOOKUP_ASSET,
)
from xiaowei_agent.capabilities.prometheus_alert import (
    OP_GET_ACTIVE_ALERTS,
    OP_QUERY_METRIC_RANGE,
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMETHEUS_ALERT_CAPABILITY_VERSION,
    PROMQL_SURFACE,
)
from xiaowei_agent.capabilities.specs import (
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    OP_LIST,
    SLOW_QUERY_SURFACE,
)
from xiaowei_agent.capabilities.target import TargetResolutionError, resolve_target
from xiaowei_agent.contracts import (
    Candidate,
    CapabilitySnapshot,
    EvidenceEnvelope,
    ExecutionPlan,
    IntentDraft,
    PlanStep,
    PolicySnapshot,
    RequestContext,
    ResolvedTarget,
    ToolResult,
)
from xiaowei_agent.evidence.asset_inventory import build_asset_evidence
from xiaowei_agent.evidence.builder import SlowQueryEvidencePolicy, build_evidence
from xiaowei_agent.evidence.errors import EvidenceBuildError
from xiaowei_agent.evidence.prometheus_alert import (
    build_alert_evidence,
    build_metric_evidence,
)
from xiaowei_agent.governance.profiles import (
    ASSET_INVENTORY_READONLY_PROFILE,
    PROMETHEUS_ALERT_READONLY_PROFILE,
    SLOW_QUERY_READONLY_PROFILE,
)
from xiaowei_agent.planning.assets.compiler import (
    compile_asset_plan,
    resolve_asset_target,
)
from xiaowei_agent.planning.assets.params import AssetLookupParams
from xiaowei_agent.planning.prometheus.compiler import (
    ALERT_LIMIT,
    compile_alert_plan,
)
from xiaowei_agent.planning.prometheus.params import (
    DEFAULT_WINDOW_MINUTES as DEFAULT_PROMETHEUS_WINDOW_MINUTES,
)
from xiaowei_agent.planning.prometheus.params import (
    PrometheusAlertParams,
)
from xiaowei_agent.planning.prometheus.params import (
    normalise_window as normalise_prometheus_window,
)
from xiaowei_agent.planning.prometheus.target import resolve_prometheus_alert_target
from xiaowei_agent.planning.prometheus.templates import metric_name_for_template
from xiaowei_agent.planning.starrocks.compiler import compile_plan
from xiaowei_agent.planning.starrocks.params import (
    DEFAULT_MIN_QUERY_TIME_MS,
    DEFAULT_ROW_LIMIT,
    DEFAULT_WINDOW_MINUTES,
    SlowQueryParams,
    normalise_window,
)
from xiaowei_agent.reflection.answerability import assess
from xiaowei_agent.reflection.asset_inventory import assess_asset_inventory
from xiaowei_agent.reflection.prometheus_alert import assess_prometheus_alert
from xiaowei_agent.rendering.asset_inventory import render_asset_inventory
from xiaowei_agent.rendering.prometheus_alert import render_prometheus_alert
from xiaowei_agent.rendering.slow_query import render
from xiaowei_agent.runners.binding import CapabilityExecutionBinding


def _window_minutes(draft: IntentDraft) -> int:
    raw = draft.slots.get("window_minutes")
    if raw is None or not raw.isdigit():
        return DEFAULT_WINDOW_MINUTES
    return int(raw)


def _prepare_slow_query(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    snapshot: CapabilitySnapshot,
) -> PreparedCapability:
    try:
        target = resolve_target(context=context, draft=draft)
        start, end = normalise_window(
            as_of=as_of, window_minutes=_window_minutes(draft)
        )
        params = SlowQueryParams(
            window_start=start,
            window_end=end,
            min_query_time_ms=DEFAULT_MIN_QUERY_TIME_MS,
            row_limit=DEFAULT_ROW_LIMIT,
            database=draft.slots.get("database"),
            user_name=draft.slots.get("user_name"),
            query_id=draft.slots.get("query_id"),
        )
        plan = compile_plan(
            candidate=candidate,
            params=params,
            target=target,
            context=context,
            snapshot=snapshot,
            surface=SLOW_QUERY_SURFACE,
        )
    except (TargetResolutionError, ValueError) as exc:
        raise CapabilityPreparationError(
            "capability parameters are outside the allowed range"
        ) from exc
    return PreparedCapability(target=target, plan=plan)


def _build_slow_query_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    return build_evidence(
        task_id=task_id,
        step=step,
        plan=plan,
        target=target,
        result=result,
        surface=SLOW_QUERY_SURFACE,
        params=SlowQueryParams.from_typed_arguments(step.typed_arguments),
        captured_at=captured_at,
    )


@dataclass(frozen=True)
class _LiveSlowQueryEvidenceBuilder:
    policy: SlowQueryEvidencePolicy

    def __call__(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        result: ToolResult,
        captured_at: dt.datetime,
    ) -> EvidenceEnvelope:
        return build_evidence(
            task_id=task_id,
            step=step,
            plan=plan,
            target=target,
            result=result,
            surface=SLOW_QUERY_SURFACE,
            params=SlowQueryParams.from_typed_arguments(step.typed_arguments),
            captured_at=captured_at,
            live_policy=self.policy,
        )


SLOW_QUERY_BINDING: Final[CapabilityRuntimeBinding] = CapabilityRuntimeBinding(
    capability_id=CAPABILITY_ID,
    capability_version=CAPABILITY_VERSION,
    entry_operation=OP_LIST,
    planner=_prepare_slow_query,
    assessor=assess,
    renderer=render,
    execution=CapabilityExecutionBinding(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        policy_profile=SLOW_QUERY_READONLY_PROFILE,
        sql_surface=SLOW_QUERY_SURFACE,
        promql_surface=None,
        evidence_builder=_build_slow_query_evidence,
    ),
)


def _prepare_prometheus_alert(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    snapshot: CapabilitySnapshot,
) -> PreparedCapability:
    if draft.missing or not {"alert_name", "instance"} <= set(draft.slots):
        raise CapabilityPreparationError(
            "capability parameters are outside the allowed range"
        )
    try:
        raw_window = draft.slots.get("window_minutes")
        window_minutes = (
            int(raw_window)
            if raw_window is not None and raw_window.isdigit()
            else DEFAULT_PROMETHEUS_WINDOW_MINUTES
        )
        start, end = normalise_prometheus_window(
            as_of=as_of, window_minutes=window_minutes
        )
        params = PrometheusAlertParams(
            alert_name=draft.slots["alert_name"],
            instance=draft.slots["instance"],
            fingerprint=draft.slots.get("fingerprint"),
            window_start=start,
            window_end=end,
        )
        target = resolve_prometheus_alert_target(context=context, params=params)
        plan = compile_alert_plan(
            candidate=candidate,
            params=params,
            target=target,
            context=context,
            snapshot=snapshot,
            surface=PROMQL_SURFACE,
        )
    except (KeyError, ValueError) as exc:
        raise CapabilityPreparationError(
            "capability parameters are outside the allowed range"
        ) from exc
    return PreparedCapability(target=target, plan=plan)


def _build_prometheus_alert_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    arguments = step.typed_arguments
    if step.operation == OP_GET_ACTIVE_ALERTS:
        alert_name = arguments.get("alert_name")
        instance = arguments.get("instance")
        fingerprint = arguments.get("fingerprint")
        limit = arguments.get("limit")
        if (
            not isinstance(alert_name, str)
            or not isinstance(instance, str)
            or (fingerprint is not None and not isinstance(fingerprint, str))
            or type(limit) is not int
            or limit != ALERT_LIMIT
        ):
            raise EvidenceBuildError
        return build_alert_evidence(
            task_id=task_id,
            step=step,
            plan=plan,
            result=result,
            captured_at=captured_at,
            expected_alert_name=alert_name,
            expected_instance=instance,
            expected_fingerprint=fingerprint,
            row_limit=limit,
        )
    if step.operation == OP_QUERY_METRIC_RANGE:
        try:
            params = PrometheusAlertParams.from_typed_arguments(arguments)
            template_id = arguments.get("promql_template_id")
            if not isinstance(template_id, str):
                raise EvidenceBuildError
            metric_name = metric_name_for_template(template_id)
        except (ValueError, EvidenceBuildError):
            raise EvidenceBuildError from None
        return build_metric_evidence(
            task_id=task_id,
            step=step,
            plan=plan,
            result=result,
            captured_at=captured_at,
            expected_instance=params.instance,
            expected_metric_name=metric_name,
            template_id=template_id,
            window_start=params.window_start,
            window_end=params.window_end,
            max_series=params.max_series,
            max_points_per_series=params.max_points_per_series,
        )
    raise EvidenceBuildError


PROMETHEUS_ALERT_BINDING: Final[CapabilityRuntimeBinding] = CapabilityRuntimeBinding(
    capability_id=PROMETHEUS_ALERT_CAPABILITY_ID,
    capability_version=PROMETHEUS_ALERT_CAPABILITY_VERSION,
    entry_operation=OP_GET_ACTIVE_ALERTS,
    planner=_prepare_prometheus_alert,
    assessor=assess_prometheus_alert,
    renderer=render_prometheus_alert,
    execution=CapabilityExecutionBinding(
        capability_id=PROMETHEUS_ALERT_CAPABILITY_ID,
        capability_version=PROMETHEUS_ALERT_CAPABILITY_VERSION,
        policy_profile=PROMETHEUS_ALERT_READONLY_PROFILE,
        sql_surface=None,
        promql_surface=PROMQL_SURFACE,
        evidence_builder=_build_prometheus_alert_evidence,
    ),
)


def _prepare_asset_inventory(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    snapshot: CapabilitySnapshot,
) -> PreparedCapability:
    if draft.missing:
        raise CapabilityPreparationError(
            "capability parameters are outside the allowed range"
        )
    try:
        params = AssetLookupParams(
            asset_id=draft.slots.get("asset_id"),
            hostname=draft.slots.get("hostname"),
            ip=draft.slots.get("ip"),
        )
        target = resolve_asset_target(context=context, params=params)
        plan = compile_asset_plan(
            candidate=candidate,
            params=params,
            target=target,
            context=context,
            snapshot=snapshot,
        )
    except ValueError as exc:
        raise CapabilityPreparationError(
            "capability parameters are outside the allowed range"
        ) from exc
    return PreparedCapability(target=target, plan=plan)


def _build_asset_inventory_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    return build_asset_evidence(
        task_id=task_id,
        step=step,
        plan=plan,
        target=target,
        result=result,
        captured_at=captured_at,
    )


ASSET_INVENTORY_BINDING: Final[CapabilityRuntimeBinding] = CapabilityRuntimeBinding(
    capability_id=ASSET_INVENTORY_CAPABILITY_ID,
    capability_version=ASSET_INVENTORY_CAPABILITY_VERSION,
    entry_operation=OP_LOOKUP_ASSET,
    planner=_prepare_asset_inventory,
    assessor=assess_asset_inventory,
    renderer=render_asset_inventory,
    execution=CapabilityExecutionBinding(
        capability_id=ASSET_INVENTORY_CAPABILITY_ID,
        capability_version=ASSET_INVENTORY_CAPABILITY_VERSION,
        policy_profile=ASSET_INVENTORY_READONLY_PROFILE,
        sql_surface=None,
        promql_surface=None,
        evidence_builder=_build_asset_inventory_evidence,
    ),
)


def build_default_capability_bindings(
    *,
    snapshot: CapabilitySnapshot,
    policy_snapshot: PolicySnapshot,
    slow_query_live_policy: SlowQueryEvidencePolicy | None = None,
) -> CapabilityBindingRegistry:
    """构造生产 binding 闭集；新增能力必须在此显式登记。"""
    slow_query_binding = SLOW_QUERY_BINDING
    if slow_query_live_policy is not None:
        slow_query_binding = replace(
            SLOW_QUERY_BINDING,
            execution=replace(
                SLOW_QUERY_BINDING.execution,
                evidence_builder=_LiveSlowQueryEvidenceBuilder(slow_query_live_policy),
            ),
        )
    return CapabilityBindingRegistry(
        snapshot=snapshot,
        policy_snapshot=policy_snapshot,
        bindings=(
            slow_query_binding,
            PROMETHEUS_ALERT_BINDING,
            ASSET_INVENTORY_BINDING,
        ),
    )

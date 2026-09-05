"""生产 capability bindings 的显式装配；不做动态发现。"""

import datetime as dt
from typing import Final

from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingRegistry,
    CapabilityPreparationError,
    CapabilityRuntimeBinding,
    PreparedCapability,
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
    ToolResult,
)
from xiaowei_agent.evidence.builder import build_evidence
from xiaowei_agent.governance.profiles import SLOW_QUERY_READONLY_PROFILE
from xiaowei_agent.planning.starrocks.compiler import compile_plan
from xiaowei_agent.planning.starrocks.params import (
    DEFAULT_MIN_QUERY_TIME_MS,
    DEFAULT_ROW_LIMIT,
    DEFAULT_WINDOW_MINUTES,
    SlowQueryParams,
    normalise_window,
)
from xiaowei_agent.reflection.answerability import assess
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
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    return build_evidence(
        task_id=task_id,
        step=step,
        plan=plan,
        result=result,
        surface=SLOW_QUERY_SURFACE,
        params=SlowQueryParams.from_typed_arguments(step.typed_arguments),
        captured_at=captured_at,
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


def build_default_capability_bindings(
    *, snapshot: CapabilitySnapshot, policy_snapshot: PolicySnapshot
) -> CapabilityBindingRegistry:
    """构造生产 binding 闭集；新增能力必须在此显式登记。"""
    return CapabilityBindingRegistry(
        snapshot=snapshot,
        policy_snapshot=policy_snapshot,
        bindings=(SLOW_QUERY_BINDING,),
    )

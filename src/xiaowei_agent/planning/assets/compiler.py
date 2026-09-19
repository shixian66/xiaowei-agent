"""资产精确查询的确定性目标与计划编译。"""

from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
    ASSET_INVENTORY_CAPABILITY_VERSION,
    ASSET_INVENTORY_ENVIRONMENT_IDS,
    OP_LOOKUP_ASSET,
)
from xiaowei_agent.capabilities.effect import build_plan_step
from xiaowei_agent.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionPlan,
    PlanBudget,
    RequestContext,
    ResolvedTarget,
)
from xiaowei_agent.planning.assets.params import AssetLookupParams

ASSET_ENTRY_OPERATION = OP_LOOKUP_ASSET
ASSET_STEP_ID = "s1"
ASSET_FIELD_SET_ID = "asset.summary.v1"
ASSET_LOOKUP_LIMIT = 2
ASSET_PROVIDER = "asset_inventory"
ASSET_RESOURCE_KIND = "asset"
ASSET_SELECTOR_VERSION = "asset.exact.v1"
ASSET_PLAN_BUDGET = PlanBudget(
    max_steps=1,
    max_tool_calls=1,
    max_model_tokens=2000,
)


def resolve_asset_target(
    *, context: RequestContext, params: AssetLookupParams
) -> ResolvedTarget:
    """只从受信 context 与已规范化 selector 构造逻辑目标。"""
    if context.environment_id not in ASSET_INVENTORY_ENVIRONMENT_IDS:
        raise ValueError("environment is outside the asset fake directory")
    selector_kind, selector_value = params.selector()
    return ResolvedTarget(
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
        provider=ASSET_PROVIDER,
        resource_kind=ASSET_RESOURCE_KIND,
        resource_ids=(f"{selector_kind}:{selector_value}",),
        selector_version=ASSET_SELECTOR_VERSION,
    )


def compile_asset_plan(
    *,
    candidate: Candidate,
    params: AssetLookupParams,
    target: ResolvedTarget,
    context: RequestContext,
    snapshot: CapabilitySnapshot,
) -> ExecutionPlan:
    """编译一个固定的资产精确查询只读步骤。"""
    if candidate.operation != ASSET_ENTRY_OPERATION:
        raise ValueError("candidate does not point at the plan entry operation")
    if (
        candidate.capability_id != ASSET_INVENTORY_CAPABILITY_ID
        or candidate.capability_version != ASSET_INVENTORY_CAPABILITY_VERSION
    ):
        raise ValueError("candidate does not identify the asset capability")
    if target != resolve_asset_target(context=context, params=params):
        raise ValueError("target or environment differs from canonical resolution")
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
    selector_kind, selector_value = params.selector()
    step = build_plan_step(
        snapshot,
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        operation=OP_LOOKUP_ASSET,
        step_id=ASSET_STEP_ID,
        typed_arguments={
            "selector_kind": selector_kind,
            "selector_value": selector_value,
            "field_set_id": ASSET_FIELD_SET_ID,
            "limit": ASSET_LOOKUP_LIMIT,
        },
    )
    return ExecutionPlan(
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        steps=(step,),
        policy_profile=spec.policy_profile,
        policy_revision=context.policy_revision,
        budget=ASSET_PLAN_BUDGET,
    )

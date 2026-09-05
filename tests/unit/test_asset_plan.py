"""资产精确查询编译成一个固定、可重放的只读步骤。"""

from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
    ASSET_INVENTORY_CAPABILITY_VERSION,
    ASSET_INVENTORY_GATEWAY,
    ASSET_INVENTORY_POLICY_PROFILE,
    OP_LOOKUP_ASSET,
)
from xiaowei_agent.capabilities.effect import derive_effect, verify_plan_effects
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    Candidate,
    IntentDraft,
    IntentSource,
    RequestContext,
    ToolCall,
)
from xiaowei_agent.planning import compute_plan_hash, compute_tool_call_hash
from xiaowei_agent.planning.assets.compiler import (
    ASSET_ENTRY_OPERATION,
    compile_asset_plan,
    resolve_asset_target,
)
from xiaowei_agent.planning.assets.params import AssetLookupParams

SNAPSHOT = StaticCapabilityRegistry().snapshot()
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-05.2",
)


def _candidate() -> Candidate:
    candidates = DeterministicCapabilityResolver().resolve(
        draft=IntentDraft(
            intent=ASSET_INVENTORY_CAPABILITY_ID,
            slots={"asset_id": "asset-1"},
            missing=(),
            confidence=0.9,
            source=IntentSource.USER,
        ),
        context=CONTEXT,
        snapshot=SNAPSHOT,
    )
    return next(item for item in candidates.items if item.operation == OP_LOOKUP_ASSET)


def _plan(params: AssetLookupParams | None = None):
    selected = AssetLookupParams(asset_id="asset-1") if params is None else params
    return compile_asset_plan(
        candidate=_candidate(),
        params=selected,
        target=resolve_asset_target(context=CONTEXT, params=selected),
        context=CONTEXT,
        snapshot=SNAPSHOT,
    )


def test_plan_has_one_fixed_readonly_step_and_closed_arguments() -> None:
    plan = _plan(AssetLookupParams(hostname="NODE-1.EXAMPLE.COM."))
    assert (plan.capability_id, plan.capability_version) == (
        ASSET_INVENTORY_CAPABILITY_ID,
        ASSET_INVENTORY_CAPABILITY_VERSION,
    )
    assert plan.policy_profile == ASSET_INVENTORY_POLICY_PROFILE
    assert plan.budget.model_dump() == {
        "max_steps": 1,
        "max_tool_calls": 1,
        "max_model_tokens": 2000,
    }
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert (step.step_id, step.operation, step.depends_on) == (
        "s1",
        OP_LOOKUP_ASSET,
        (),
    )
    assert dict(step.typed_arguments) == {
        "selector_kind": "hostname",
        "selector_value": "node-1.example.com",
        "field_set_id": "asset.summary.v1",
        "limit": 2,
    }
    verify_plan_effects(SNAPSHOT, plan)


def test_plan_and_tool_hashes_are_stable_for_identical_inputs() -> None:
    first = _plan()
    second = _plan()
    assert first == second
    assert compute_plan_hash(first) == compute_plan_hash(second)
    operation = derive_effect(
        SNAPSHOT,
        capability_id=first.capability_id,
        capability_version=first.capability_version,
        operation=first.steps[0].operation,
    )
    assert operation.gateway == ASSET_INVENTORY_GATEWAY
    calls = tuple(
        ToolCall(
            gateway=operation.gateway,
            operation=plan.steps[0].operation,
            step_id=plan.steps[0].step_id,
            typed_args=plan.steps[0].typed_arguments,
            timeout_seconds=30.0,
            idempotency_key="fixed:s1",
        )
        for plan in (first, second)
    )
    assert compute_tool_call_hash(calls[0]) == compute_tool_call_hash(calls[1])


def test_selector_changes_plan_hash() -> None:
    assert compute_plan_hash(_plan(AssetLookupParams(asset_id="asset-1"))) != (
        compute_plan_hash(_plan(AssetLookupParams(asset_id="asset-2")))
    )


def test_non_entry_candidate_and_target_drift_are_rejected() -> None:
    candidate = _candidate().model_copy(update={"operation": "undeclared"})
    params = AssetLookupParams(asset_id="asset-1")
    target = resolve_asset_target(context=CONTEXT, params=params)
    try:
        compile_asset_plan(
            candidate=candidate,
            params=params,
            target=target,
            context=CONTEXT,
            snapshot=SNAPSHOT,
        )
    except ValueError as exc:
        assert "entry operation" in str(exc)
    else:
        raise AssertionError("non-entry candidate must be rejected")

    other_context = CONTEXT.model_copy(update={"environment_id": "test"})
    other_target = resolve_asset_target(context=other_context, params=params)
    try:
        compile_asset_plan(
            candidate=_candidate(),
            params=params,
            target=other_target,
            context=CONTEXT,
            snapshot=SNAPSHOT,
        )
    except ValueError as exc:
        assert "environment" in str(exc)
    else:
        raise AssertionError("target drift must be rejected")


def test_entry_operation_is_the_declared_lookup() -> None:
    assert ASSET_ENTRY_OPERATION == OP_LOOKUP_ASSET

"""F1 PlanCompiler：计划只保存引用、hash、目标与预算（设计 §5.5）。"""

import hashlib

import pytest
from pydantic import ValidationError

from xiaowei_agent.capabilities.readonly_query import (
    OP_EXECUTE_READONLY_QUERY,
    READONLY_QUERY_CAPABILITY_ID,
    READONLY_QUERY_CAPABILITY_VERSION,
    READONLY_QUERY_POLICY_PROFILE,
    READONLY_QUERY_SPEC,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    Candidate,
    EffectClass,
    PlanBudget,
    ReadClass,
    RequestContext,
)
from xiaowei_agent.contracts.enums import QueryRequirement
from xiaowei_agent.contracts.intent import (
    INTENT_SLOT_ALLOWLISTS,
    READONLY_QUERY_INTENT,
    RULE_ONLY_INTENTS,
)
from xiaowei_agent.contracts.model import ProviderIntentResponse, ProviderIntentSlots
from xiaowei_agent.contracts.sql_query import (
    CONFIRMED_ARTIFACT_ARGUMENT_KEYS,
    ReadonlyQueryBudget,
)
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from xiaowei_agent.planning.starrocks.readonly_query import (
    READONLY_QUERY_PLAN_BUDGET,
    ReadonlyQueryParams,
    compile_readonly_query_plan,
    resolve_readonly_query_target,
)

_SQL_HASH = hashlib.sha256(b"SELECT secret_column FROM t").hexdigest()
_CONTEXT = RequestContext(
    tenant_id="t1",
    actor="alice",
    environment_id="test",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-05.2",
)
_CANDIDATE = Candidate(
    capability_id=READONLY_QUERY_CAPABILITY_ID,
    capability_version=READONLY_QUERY_CAPABILITY_VERSION,
    operation=OP_EXECUTE_READONLY_QUERY,
    score=1.0,
    match_evidence=("intent equals the declared capability id",),
    required_context=("tenant_id", "environment_id"),
)
_BUDGET = ReadonlyQueryBudget(
    preview_max_rows=1000, preview_max_bytes=20_971_520, query_timeout_seconds=180
)
_PARAMS = ReadonlyQueryParams(
    sql_ref="ref-1",
    sql_hash=_SQL_HASH,
    resource_id="a" * 32,
    config_revision="c" * 64,
    budget=_BUDGET,
)
_SNAPSHOT = StaticCapabilityRegistry().snapshot()


def _plan(params: ReadonlyQueryParams = _PARAMS, context: RequestContext = _CONTEXT):  # type: ignore[no-untyped-def]
    target = resolve_readonly_query_target(context=context, params=params)
    return compile_readonly_query_plan(
        candidate=_CANDIDATE,
        params=params,
        target=target,
        context=context,
        snapshot=_SNAPSHOT,
    ), target


def test_the_operation_is_a_restricted_read_requiring_a_confirmed_artifact() -> None:
    (operation,) = READONLY_QUERY_SPEC.operations
    assert operation.operation == OP_EXECUTE_READONLY_QUERY
    assert operation.effect_class is EffectClass.READ
    assert operation.read_class is ReadClass.RESTRICTED
    assert operation.side_effect is False
    assert operation.query_requirement is QueryRequirement.CONFIRMED_ARTIFACT
    assert READONLY_QUERY_SPEC.policy_profile == READONLY_QUERY_POLICY_PROFILE
    assert READONLY_QUERY_SPEC in _SNAPSHOT.specs


def test_the_plan_has_one_step_of_references_and_budget_only() -> None:
    plan, target = _plan()
    (step,) = plan.steps
    assert step.operation == OP_EXECUTE_READONLY_QUERY
    assert set(step.typed_arguments) == CONFIRMED_ARTIFACT_ARGUMENT_KEYS
    assert step.typed_arguments == {
        "sql_ref": "ref-1",
        "sql_hash": _SQL_HASH,
        "resource_id": "a" * 32,
        "target_fingerprint": compute_target_fingerprint(target),
        "config_revision": "c" * 64,
        "preview_max_rows": 1000,
        "preview_max_bytes": 20_971_520,
        "query_timeout_seconds": 180,
    }
    assert step.read_class is ReadClass.RESTRICTED
    assert plan.budget == PlanBudget(max_steps=1, max_tool_calls=1, max_model_tokens=0)
    assert plan.budget == READONLY_QUERY_PLAN_BUDGET
    assert target.resource_ids == ("a" * 32,)
    assert (target.tenant_id, target.environment_id) == ("t1", "test")


def test_sql_text_never_enters_the_plan() -> None:
    plan, _ = _plan()
    dumped = plan.model_dump_json()
    assert "secret_column" not in dumped
    assert "SELECT" not in dumped


@pytest.mark.parametrize(
    "change",
    [
        {"preview_max_rows": 999},
        {"preview_max_bytes": 20_971_519},
        {"query_timeout_seconds": 179},
    ],
)
def test_budget_and_references_enter_the_plan_hash(change: dict[str, int]) -> None:
    plan, _ = _plan()
    baseline = compute_plan_hash(plan)
    assert compute_plan_hash(_plan()[0]) == baseline
    enlarged = _PARAMS.model_copy(
        update={"budget": _BUDGET.model_copy(update=change)}
    )
    assert compute_plan_hash(_plan(enlarged)[0]) != baseline
    for field, value in (
        ("sql_ref", "ref-2"),
        ("sql_hash", "d" * 64),
        ("resource_id", "b" * 32),
        ("config_revision", "e" * 64),
    ):
        changed = _PARAMS.model_copy(update={field: value})
        assert compute_plan_hash(_plan(changed)[0]) != baseline


def test_an_amplified_stored_budget_is_detected_by_recompilation() -> None:
    plan, _ = _plan()
    (step,) = plan.steps
    tampered = plan.model_copy(
        update={
            "steps": (
                step.model_copy(
                    update={
                        "typed_arguments": {
                            **step.typed_arguments,
                            "preview_max_rows": 100_000,
                        }
                    }
                ),
            )
        }
    )
    assert compute_plan_hash(tampered) != compute_plan_hash(plan)


def test_params_reject_budgets_above_the_hard_limits() -> None:
    with pytest.raises(ValidationError):
        ReadonlyQueryParams.model_validate(
            {
                **_PARAMS.model_dump(),
                "budget": {
                    "preview_max_rows": 1001,
                    "preview_max_bytes": 20_971_520,
                    "query_timeout_seconds": 180,
                },
            }
        )


def test_the_intent_is_rule_only_and_never_model_selectable() -> None:
    assert READONLY_QUERY_INTENT == READONLY_QUERY_CAPABILITY_ID
    assert READONLY_QUERY_INTENT in RULE_ONLY_INTENTS
    assert READONLY_QUERY_INTENT not in INTENT_SLOT_ALLOWLISTS
    with pytest.raises(ValidationError):
        ProviderIntentResponse(
            intent=READONLY_QUERY_INTENT,
            slots=ProviderIntentSlots(),
            missing=(),
            confidence=0.9,
        )

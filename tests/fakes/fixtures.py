"""固定夹具。

写能力 ``test.synthetic.write`` **仅用于伪标反证**：它不注册任何真实 adapter，
也永远不会被 Gateway 执行（M0-M7 的 E1 硬闸）。
"""

from xiaowei_agent.contracts import (
    CapabilitySnapshot,
    CapabilitySpec,
    EffectClass,
    ExecutionPlan,
    OperationSpec,
    PlanBudget,
    PlanStep,
    ResolvedTarget,
    ToolCall,
)

READ_CAP = "starrocks.slow_query.diagnose"
READ_OP = "list_slow_queries"
WRITE_CAP = "test.synthetic.write"
WRITE_OP = "mutate_probe"
CAP_VERSION = "1.0.0"

SNAPSHOT = CapabilitySnapshot(
    snapshot_id="snap-m2-fixture",
    specs=(
        CapabilitySpec(
            capability_id=READ_CAP,
            version=CAP_VERSION,
            domain="starrocks",
            operations=(
                OperationSpec(
                    operation=READ_OP,
                    gateway="starrocks",
                    effect_class=EffectClass.READ,
                    side_effect=False,
                    argument_schema_ref="schema.starrocks.slow_query.v1",
                ),
            ),
            policy_profile="readonly.default",
            evidence_contract="evidence.starrocks.slow_query.v1",
            eval_ref="evals.starrocks.slow_query",
        ),
        CapabilitySpec(
            capability_id=WRITE_CAP,
            version=CAP_VERSION,
            domain="test",
            operations=(
                OperationSpec(
                    operation=WRITE_OP,
                    gateway="test",
                    effect_class=EffectClass.MUTATE_TARGET,
                    side_effect=True,
                    argument_schema_ref="schema.test.write.v1",
                ),
            ),
            policy_profile="write.synthetic",
            evidence_contract="evidence.test.write.v1",
            eval_ref="evals.test.write",
        ),
    ),
)


BUDGET = PlanBudget(max_steps=4, max_tool_calls=4, max_model_tokens=8000)

FIXTURE_PLAN = ExecutionPlan(
    capability_id=READ_CAP,
    capability_version=CAP_VERSION,
    steps=(
        PlanStep(
            step_id="s1",
            operation=READ_OP,
            typed_arguments={"window_minutes": 30},
            depends_on=(),
            side_effect=False,
            effect_class=EffectClass.READ,
        ),
    ),
    policy_profile="readonly.default",
    policy_revision="policy-2026-09-01",
    budget=BUDGET,
)

FIXTURE_TARGET = ResolvedTarget(
    tenant_id="dev-local",
    environment_id="dev",
    provider="starrocks",
    resource_kind="cluster",
    resource_ids=("c1", "c2"),
    selector_version="1",
)

FIXTURE_TOOL_CALL = ToolCall(
    gateway="starrocks",
    operation=READ_OP,
    step_id="s1",
    typed_args={"window_minutes": 30},
    timeout_seconds=30.0,
    idempotency_key="idem-1",
)

"""StepAdmission 按 ``query_requirement`` 强制 SQLGuard profile（设计 §5.6、§7.1，计划 Task 7）。

F1 capability 在 Task 8 才注册；这里用只存在于测试的合成能力声明 ``CONFIRMED_ARTIFACT``，
其余快照取生产 registry，因此慢查询的 ``TEMPLATE_LOCKED`` 声明是真实声明。
"""

import hashlib
from typing import Any, Final

import pytest
from tests.fakes.admission import (
    CONTEXT,
    NOW,
    POLICY_REVISION,
    REGISTRY_SNAPSHOT,
    TARGET,
    TASK_ID,
    slow_query_call,
    slow_query_plan,
    slow_query_step,
)

from xiaowei_agent.capabilities.effect import build_plan_step
from xiaowei_agent.capabilities.specs import CAPABILITY_ID, SLOW_QUERY_SURFACE
from xiaowei_agent.contracts import (
    AdmissionCertificate,
    CapabilitySnapshot,
    CapabilitySpec,
    EffectClass,
    ExecutionPlan,
    OperationSpec,
    PlanBudget,
    PlanStep,
    PolicyProfile,
    PolicySnapshot,
    ReadClass,
    ResolvedTarget,
    RiskLevel,
    SqlGuardRejection,
    ToolCall,
)
from xiaowei_agent.contracts.enums import QueryRequirement
from xiaowei_agent.contracts.sql_query import (
    CONFIRMED_ARTIFACT_ARGUMENT_KEYS,
    HydratedQuery,
    QualifiedRelation,
    ReadonlyQueryBudget,
    confirmed_artifact_arguments,
)
from xiaowei_agent.governance.approval import NeverGrantingApprovalGate
from xiaowei_agent.governance.profiles import SLOW_QUERY_READONLY_PROFILE
from xiaowei_agent.governance.sqlguard import ReadonlyPolicy, SqlGuardError
from xiaowei_agent.governance.step_admission import admit_step
from xiaowei_agent.planning import compute_target_fingerprint, compute_tool_call_hash

pytestmark = pytest.mark.security

F1_CAP: Final[str] = "test.f1.readonly_query"
F1_OP: Final[str] = "run_readonly_query"
F1_GATEWAY: Final[str] = "starrocks_query"
F1_PROFILE_ID: Final[str] = "readonly.test.f1"

F1_SPEC = CapabilitySpec(
    capability_id=F1_CAP,
    version="1.0.0",
    domain="starrocks",
    input_schema_ref="input.test.f1.v1",
    operations=(
        OperationSpec(
            operation=F1_OP,
            gateway=F1_GATEWAY,
            effect_class=EffectClass.READ,
            read_class=ReadClass.BOUNDED,
            side_effect=False,
            argument_schema_ref="schema.test.f1.v1",
            query_requirement=QueryRequirement.CONFIRMED_ARTIFACT,
        ),
    ),
    policy_profile=F1_PROFILE_ID,
    evidence_contract="evidence.test.f1.v1",
    eval_ref="evals.test.f1",
)
SNAPSHOT = CapabilitySnapshot(
    snapshot_id="snap-f1-admission", specs=(*REGISTRY_SNAPSHOT.specs, F1_SPEC)
)
F1_PROFILE = PolicyProfile(
    profile_id=F1_PROFILE_ID,
    allowed_operations=(F1_OP,),
    allowed_effect_classes=(EffectClass.READ,),
    allowed_read_classes=(ReadClass.BOUNDED,),
    allowed_environment_ids=("dev",),
    risk=RiskLevel.LOW,
    max_timeout_seconds=300.0,
)
POLICY_SNAPSHOT = PolicySnapshot(
    policy_revision=POLICY_REVISION,
    profiles=(F1_PROFILE_ID, SLOW_QUERY_READONLY_PROFILE.profile_id),
)

F1_TARGET = ResolvedTarget(
    tenant_id="dev-local",
    environment_id="dev",
    provider="starrocks",
    resource_kind="starrocks_cluster",
    resource_ids=("sr-dev",),
    selector_version="v1",
)
OTHER_TARGET = F1_TARGET.model_copy(update={"resource_ids": ("sr-other",)})
READONLY_POLICY = ReadonlyPolicy(
    default_database="app",
    blocked_relation_names=frozenset({QualifiedRelation(database="app", name="secret")}),
)
BUDGET = ReadonlyQueryBudget(
    preview_max_rows=1000, preview_max_bytes=20_971_520, query_timeout_seconds=180
)


def _hydrated(sql: bytes = b"SELECT a FROM t", **overrides: Any) -> HydratedQuery:
    values: dict[str, Any] = {
        "sql_ref": "ref-1",
        "sql_hash": hashlib.sha256(sql).hexdigest(),
        "sql_bytes": sql,
        "resource_id": "sr-dev",
        "target_fingerprint": compute_target_fingerprint(F1_TARGET),
        "config_revision": "cfg-1",
        "budget": BUDGET,
    }
    return HydratedQuery(**(values | overrides))


def _step(arguments: dict[str, Any]) -> PlanStep:
    return build_plan_step(
        SNAPSHOT,
        capability_id=F1_CAP,
        capability_version="1.0.0",
        operation=F1_OP,
        step_id="s1",
        typed_arguments=arguments,
    )


def _plan(step: PlanStep) -> ExecutionPlan:
    return ExecutionPlan(
        capability_id=F1_CAP,
        capability_version="1.0.0",
        steps=(step,),
        policy_profile=F1_PROFILE_ID,
        policy_revision=POLICY_REVISION,
        budget=PlanBudget(max_steps=1, max_tool_calls=1, max_model_tokens=1),
    )


def _call(arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        gateway=F1_GATEWAY,
        operation=F1_OP,
        step_id="s1",
        typed_args=arguments,
        timeout_seconds=200.0,
        idempotency_key="idem-f1",
    )


def _admit_f1(
    *,
    query: HydratedQuery | None,
    step_arguments: dict[str, Any] | None = None,
    call_arguments: dict[str, Any] | None = None,
    target: ResolvedTarget = F1_TARGET,
    readonly_policy: ReadonlyPolicy | None = READONLY_POLICY,
) -> AdmissionCertificate:
    arguments = _hydrated().tool_arguments()
    step = _step(step_arguments if step_arguments is not None else arguments)
    return admit_step(
        step=step,
        plan=_plan(step),
        call=_call(call_arguments if call_arguments is not None else arguments),
        context=CONTEXT,
        target=target,
        snapshot=SNAPSHOT,
        policy_snapshot=POLICY_SNAPSHOT,
        profile=F1_PROFILE,
        sql_surface=None,
        promql_surface=None,
        approval_gate=NeverGrantingApprovalGate(),
        task_id=TASK_ID,
        now=NOW,
        hydrated_query=query,
        readonly_policy=readonly_policy,
    )


def _rejection(**kwargs: Any) -> SqlGuardRejection:
    with pytest.raises(SqlGuardError) as caught:
        _admit_f1(**kwargs)
    return caught.value.rejection


def _admit_slow_query(**kwargs: Any) -> AdmissionCertificate:
    step = kwargs.pop("step", slow_query_step())
    return admit_step(
        step=step,
        plan=kwargs.pop("plan", slow_query_plan()),
        call=kwargs.pop("call", slow_query_call()),
        context=CONTEXT,
        target=TARGET,
        snapshot=SNAPSHOT,
        policy_snapshot=POLICY_SNAPSHOT,
        profile=SLOW_QUERY_READONLY_PROFILE,
        sql_surface=SLOW_QUERY_SURFACE,
        promql_surface=None,
        approval_gate=NeverGrantingApprovalGate(),
        task_id=TASK_ID,
        now=NOW,
        **kwargs,
    )


# ---- 成功对照 ----


def test_a_bound_readonly_query_is_admitted() -> None:
    arguments = _hydrated().tool_arguments()
    certificate = _admit_f1(query=_hydrated())
    assert certificate.tool_call_hash == compute_tool_call_hash(_call(arguments))
    assert set(arguments) == CONFIRMED_ARTIFACT_ARGUMENT_KEYS


def test_slow_query_template_path_is_unchanged() -> None:
    _admit_slow_query()


def test_the_slow_query_operations_declare_template_locked() -> None:
    declared = {
        op.operation: op.query_requirement
        for spec in REGISTRY_SNAPSHOT.specs
        if spec.capability_id == CAPABILITY_ID
        for op in spec.operations
    }
    assert set(declared.values()) == {QueryRequirement.TEMPLATE_LOCKED}


# ---- requirement：缺失与多余 ----


def test_required_query_missing_is_rejected() -> None:
    assert _rejection(query=None) is SqlGuardRejection.QUERY_REQUIREMENT_MISMATCH
    assert (
        _rejection(query=_hydrated(), readonly_policy=None)
        is SqlGuardRejection.QUERY_REQUIREMENT_MISMATCH
    )


def test_a_query_passed_to_a_template_step_is_rejected() -> None:
    with pytest.raises(SqlGuardError) as caught:
        _admit_slow_query(hydrated_query=_hydrated(), readonly_policy=READONLY_POLICY)
    assert caught.value.rejection is SqlGuardRejection.QUERY_REQUIREMENT_MISMATCH


def test_a_slow_query_plan_disguised_as_confirmed_readonly_is_rejected() -> None:
    # 慢查询计划把 SQL 信封换成 F1 标量并附上 HydratedQuery：profile 来自快照而不是步骤，
    # 仍按 TEMPLATE_LOCKED 处理。
    arguments = _hydrated().tool_arguments()
    step = slow_query_step().model_copy(update={"typed_arguments": arguments})
    plan = slow_query_plan(steps=(step,))
    call = slow_query_call(typed_args=arguments)
    for hydrated in (_hydrated(), None):
        with pytest.raises(SqlGuardError) as caught:
            _admit_slow_query(
                step=step,
                plan=plan,
                call=call,
                hydrated_query=hydrated,
                readonly_policy=READONLY_POLICY,
            )
        assert caught.value.rejection is SqlGuardRejection.QUERY_REQUIREMENT_MISMATCH


def test_a_confirmed_step_carrying_a_template_envelope_is_rejected() -> None:
    arguments = _hydrated().tool_arguments() | {
        "sql": "SELECT 1",
        "sql_template_id": "slow_query.list.v1",
    }
    assert (
        _rejection(query=_hydrated(), step_arguments=arguments, call_arguments=arguments)
        is SqlGuardRejection.QUERY_REQUIREMENT_MISMATCH
    )


def test_a_none_operation_may_not_carry_sql() -> None:
    # 资产查询声明 NONE：带 SQL 信封或 HydratedQuery 都拒绝。
    from xiaowei_agent.capabilities.asset_inventory import (
        ASSET_INVENTORY_CAPABILITY_ID,
        ASSET_INVENTORY_POLICY_PROFILE,
        OP_LOOKUP_ASSET,
    )
    from xiaowei_agent.governance.profiles import ASSET_INVENTORY_READONLY_PROFILE

    spec = next(s for s in SNAPSHOT.specs if s.capability_id == ASSET_INVENTORY_CAPABILITY_ID)
    op = next(o for o in spec.operations if o.operation == OP_LOOKUP_ASSET)
    assert op.query_requirement is QueryRequirement.NONE
    for arguments, hydrated in (
        ({"sql": "SELECT 1", "sql_template_id": "x"}, None),
        ({"asset_ref": "a"}, _hydrated()),
    ):
        step = build_plan_step(
            SNAPSHOT,
            capability_id=ASSET_INVENTORY_CAPABILITY_ID,
            capability_version=spec.version,
            operation=OP_LOOKUP_ASSET,
            step_id="s1",
            typed_arguments=arguments,
        )
        plan = ExecutionPlan(
            capability_id=ASSET_INVENTORY_CAPABILITY_ID,
            capability_version=spec.version,
            steps=(step,),
            policy_profile=ASSET_INVENTORY_POLICY_PROFILE,
            policy_revision=POLICY_REVISION,
            budget=PlanBudget(max_steps=1, max_tool_calls=1, max_model_tokens=1),
        )
        call = ToolCall(
            gateway=op.gateway,
            operation=OP_LOOKUP_ASSET,
            step_id="s1",
            typed_args=arguments,
            timeout_seconds=10.0,
            idempotency_key="idem-asset",
        )
        with pytest.raises(SqlGuardError) as caught:
            admit_step(
                step=step,
                plan=plan,
                call=call,
                context=CONTEXT,
                target=TARGET,
                snapshot=SNAPSHOT,
                policy_snapshot=PolicySnapshot(
                    policy_revision=POLICY_REVISION,
                    profiles=(ASSET_INVENTORY_POLICY_PROFILE,),
                ),
                profile=ASSET_INVENTORY_READONLY_PROFILE.model_copy(
                    update={"allowed_environment_ids": ("dev",)}
                ),
                sql_surface=SLOW_QUERY_SURFACE,
                promql_surface=None,
                approval_gate=NeverGrantingApprovalGate(),
                task_id=TASK_ID,
                now=NOW,
                hydrated_query=hydrated,
                readonly_policy=READONLY_POLICY,
            )
        assert caught.value.rejection is SqlGuardRejection.QUERY_REQUIREMENT_MISMATCH


# ---- 标量逐项一致 ----


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sql_ref", "ref-2"),
        ("sql_hash", hashlib.sha256(b"SELECT 2").hexdigest()),
        ("resource_id", "sr-other"),
        ("target_fingerprint", compute_target_fingerprint(OTHER_TARGET)),
        ("config_revision", "cfg-2"),
        ("preview_max_rows", 999),
        ("preview_max_bytes", 1_048_576),
        ("query_timeout_seconds", 179),
        ("preview_max_rows", True),
        ("query_timeout_seconds", 180.0),
    ],
)
def test_any_scalar_that_differs_from_the_hydrated_query_is_rejected(
    key: str, value: object
) -> None:
    changed = _hydrated().tool_arguments() | {key: value}
    for location in ("step_arguments", "call_arguments"):
        assert (
            _rejection(query=_hydrated(), **{location: changed})
            is SqlGuardRejection.QUERY_BINDING_MISMATCH
        )


@pytest.mark.parametrize("key", sorted(CONFIRMED_ARTIFACT_ARGUMENT_KEYS))
def test_a_missing_scalar_is_rejected(key: str) -> None:
    reduced = {k: v for k, v in _hydrated().tool_arguments().items() if k != key}
    assert (
        _rejection(query=_hydrated(), step_arguments=reduced, call_arguments=reduced)
        is SqlGuardRejection.QUERY_BINDING_MISMATCH
    )


def test_an_extra_scalar_is_rejected() -> None:
    extended = _hydrated().tool_arguments() | {"database": "ops"}
    assert (
        _rejection(query=_hydrated(), step_arguments=extended, call_arguments=extended)
        is SqlGuardRejection.QUERY_BINDING_MISMATCH
    )


def test_the_hydrated_query_must_belong_to_the_resolved_target() -> None:
    # 计划、调用与 HydratedQuery 彼此一致，但都指向另一个 target。
    assert (
        _rejection(query=_hydrated(), target=OTHER_TARGET)
        is SqlGuardRejection.QUERY_BINDING_MISMATCH
    )


def test_a_target_with_the_same_resource_but_another_fingerprint_is_rejected() -> None:
    # 同一 resource_id，但 selector 版本不同：只有目标指纹能发现。
    drifted = F1_TARGET.model_copy(update={"selector_version": "v2"})
    assert drifted.resource_ids == F1_TARGET.resource_ids
    assert (
        _rejection(query=_hydrated(), target=drifted)
        is SqlGuardRejection.QUERY_BINDING_MISMATCH
    )


def test_a_query_for_another_resource_is_rejected_even_with_the_right_fingerprint() -> None:
    # 指纹指向本 target，但 HydratedQuery 自称另一个 resource：只有 resource_id 能发现。
    query = _hydrated(resource_id="sr-other")
    arguments = query.tool_arguments()
    assert (
        _rejection(query=query, step_arguments=arguments, call_arguments=arguments)
        is SqlGuardRejection.QUERY_BINDING_MISMATCH
    )


def test_the_hydrated_query_must_be_the_one_the_plan_references() -> None:
    other = _hydrated(b"SELECT b FROM t")
    assert _rejection(query=other) is SqlGuardRejection.QUERY_BINDING_MISMATCH


# ---- SQLGuard 在准入里真正运行 ----


@pytest.mark.parametrize(
    ("sql", "rejection"),
    [
        (b"DROP TABLE t", SqlGuardRejection.NOT_READONLY),
        (b"SELECT * FROM secret", SqlGuardRejection.BLOCKED_RELATION),
        (b"SELECT 1; SELECT 2", SqlGuardRejection.MULTIPLE_STATEMENTS),
        (b"SHOW USERS", SqlGuardRejection.READONLY_STATEMENT_NOT_SUPPORTED),
    ],
)
def test_the_confirmed_readonly_guard_runs_inside_admission(
    sql: bytes, rejection: SqlGuardRejection
) -> None:
    query = _hydrated(sql)
    arguments = query.tool_arguments()
    assert (
        _rejection(query=query, step_arguments=arguments, call_arguments=arguments)
        is rejection
    )


# ---- tool_call_hash 覆盖全部标量 ----


def test_tool_call_hash_covers_every_scalar() -> None:
    arguments = _hydrated().tool_arguments()
    base = compute_tool_call_hash(_call(arguments))
    replacements: dict[str, object] = {
        "sql_ref": "ref-2",
        "sql_hash": "0" * 64,
        "resource_id": "sr-other",
        "target_fingerprint": "1" * 64,
        "config_revision": "cfg-2",
        "preview_max_rows": 999,
        "preview_max_bytes": 1_048_577,
        "query_timeout_seconds": 1,
    }
    assert set(replacements) == CONFIRMED_ARTIFACT_ARGUMENT_KEYS
    for key, value in replacements.items():
        assert compute_tool_call_hash(_call(arguments | {key: value})) != base


def test_arguments_helper_matches_the_hydrated_query() -> None:
    query = _hydrated()
    assert query.tool_arguments() == confirmed_artifact_arguments(
        sql_ref=query.sql_ref,
        sql_hash=query.sql_hash,
        resource_id=query.resource_id,
        target_fingerprint=query.target_fingerprint,
        config_revision=query.config_revision,
        budget=query.budget,
    )
    assert "SELECT" not in repr(query.tool_arguments())


"""三个指纹的固定向量。

期望值写成规范 JSON **字节串**而非不透明摘要：字节串把键序、分隔符、空值表示与
数字格式一并钉死，任何 canonicalization 改动都会在这里立刻可读地暴露出来。
"""

import datetime as dt
import hashlib

import pytest
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET, FIXTURE_TOOL_CALL

from xiaowei_agent.contracts import (
    Channel,
    EffectClass,
    ExecutionPlan,
    ReadClass,
    RequestContext,
    RequestEnvelope,
    StepCondition,
    StepConditionKind,
    TaskSubmission,
)
from xiaowei_agent.persistence.store import (
    idempotency_scope_digest,
    request_dedup_digest,
    submission_digest,
)
from xiaowei_agent.planning import (
    compute_plan_hash,
    compute_target_fingerprint,
    compute_tool_call_hash,
)

_PLAN_CANONICAL = (
    b'{"budget":{"max_model_tokens":8000,"max_steps":4,"max_tool_calls":4},'
    b'"capability_id":"starrocks.slow_query.diagnose","capability_version":"1.0.0",'
    b'"ordered_steps":[{"condition":{"expected_result":null,"field":null,'
    b'"kind":"always","ref_step_id":null,"threshold":null},"depends_on":[],'
    b'"effect_class":"read","operation":"list_slow_queries","read_class":"bounded",'
    b'"side_effect":false,"step_id":"s1","typed_arguments":{"window_minutes":30}}],'
    b'"plan_schema_version":2,"policy_profile":"readonly.default",'
    b'"policy_revision":"policy-2026-09-01"}'
)

_TARGET_CANONICAL = (
    b'{"environment_id":"dev","provider":"starrocks","resource_ids":["c1","c2"],'
    b'"resource_kind":"cluster","selector_version":"1","tenant_id":"dev-local"}'
)

_TOOL_CALL_CANONICAL = (
    b'{"gateway":"starrocks","idempotency_key":"idem-1",'
    b'"operation":"list_slow_queries","step_id":"s1","timeout_seconds":30.0,'
    b'"typed_args":{"window_minutes":30}}'
)

_REQUEST_DEDUP_CANONICAL = (
    b'{"actor":"alice","channel":"api","environment_id":"dev",'
    b'"idempotency_key":"idem-1","tenant_id":"tenant-a",'
    b'"text":"why is the query slow"}'
)
_IDEMPOTENCY_SCOPE_CANONICAL = (
    b'{"environment_id":"dev","idempotency_key":"idem-1",'
    b'"tenant_id":"tenant-a"}'
)
_SUBMISSION_CANONICAL = (
    b'{"as_of":"2026-09-05T09:30:00+00:00","context":{"actor":"alice",'
    b'"environment_id":"dev","policy_revision":"policy-1",'
    b'"tenant_id":"tenant-a","trace_id":"00000000000000000000000000000000"},'
    b'"envelope":{"actor":"alice","channel":"api","environment_id":"dev",'
    b'"idempotency_key":"idem-1","request_id":"request-1",'
    b'"tenant_id":"tenant-a","text":"why is the query slow"}}'
)


def _null_parent_submission() -> TaskSubmission:
    return TaskSubmission(
        envelope=RequestEnvelope(
            request_id="request-1",
            tenant_id="tenant-a",
            actor="alice",
            channel=Channel.API,
            text="why is the query slow",
            idempotency_key="idem-1",
            environment_id="dev",
        ),
        context=RequestContext(
            tenant_id="tenant-a",
            actor="alice",
            environment_id="dev",
            trace_id="0" * 32,
            policy_revision="policy-1",
        ),
        as_of=dt.datetime(2026, 9, 5, 9, 30, tzinfo=dt.UTC),
        clarification_parent_task_id=None,
    )


def test_null_parent_keeps_all_pre_ri3_digest_bytes_frozen() -> None:
    submission = _null_parent_submission()
    assert request_dedup_digest(
        submission.envelope,
        submission.context,
        clarification_parent_task_id=None,
    ) == hashlib.sha256(_REQUEST_DEDUP_CANONICAL).hexdigest()
    assert submission_digest(submission) == hashlib.sha256(
        _SUBMISSION_CANONICAL
    ).hexdigest()
    assert idempotency_scope_digest(
        tenant_id="tenant-a",
        environment_id="dev",
        idempotency_key="idem-1",
    ) == hashlib.sha256(_IDEMPOTENCY_SCOPE_CANONICAL).hexdigest()


def test_plan_hash_matches_the_frozen_canonical_form() -> None:
    assert compute_plan_hash(FIXTURE_PLAN) == hashlib.sha256(_PLAN_CANONICAL).hexdigest()


def test_target_fingerprint_matches_the_frozen_canonical_form() -> None:
    expected = hashlib.sha256(_TARGET_CANONICAL).hexdigest()
    assert compute_target_fingerprint(FIXTURE_TARGET) == expected


def test_tool_call_hash_matches_the_frozen_canonical_form() -> None:
    expected = hashlib.sha256(_TOOL_CALL_CANONICAL).hexdigest()
    assert compute_tool_call_hash(FIXTURE_TOOL_CALL) == expected


def test_hashes_are_stable_across_repeated_calls() -> None:
    assert compute_plan_hash(FIXTURE_PLAN) == compute_plan_hash(FIXTURE_PLAN)


def _plan_with_step(**overrides: object) -> ExecutionPlan:
    step = FIXTURE_PLAN.steps[0].model_copy(update=overrides)
    return FIXTURE_PLAN.model_copy(update={"steps": (step,)})


def test_changing_effect_class_changes_plan_hash() -> None:
    """分类漂移必须被 plan_hash 检出（ADR-009 D1）。"""
    mutated = _plan_with_step(
        effect_class=EffectClass.MUTATE_TARGET,
        read_class=None,
        side_effect=True,
    )
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_changing_read_class_changes_plan_hash() -> None:
    mutated = _plan_with_step(read_class=ReadClass.RESTRICTED)
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_changing_condition_changes_plan_hash(two_step_plan: ExecutionPlan) -> None:
    """可选分支的条件是计划的一部分。

    条件只能引用**更早**的步骤，因此必须在两步计划上验证——单步计划里条件引用
    自身会被 ExecutionPlan 的校验器直接拒绝（这本身也是正确行为）。
    """
    first, second = two_step_plan.steps
    conditional = second.model_copy(
        update={
            "condition": StepCondition(
                kind=StepConditionKind.EVIDENCE_ROW_COUNT_BELOW,
                ref_step_id="s1",
                threshold=1,
            )
        }
    )
    mutated = two_step_plan.model_copy(update={"steps": (first, conditional)})
    assert compute_plan_hash(mutated) != compute_plan_hash(two_step_plan)


def test_condition_cannot_reference_its_own_step() -> None:
    """自引用条件在计划校验期即失败，不需要 hash 层再处理。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _plan_with_step(
            condition=StepCondition(
                kind=StepConditionKind.EVIDENCE_ROW_COUNT_BELOW,
                ref_step_id="s1",
                threshold=1,
            )
        )


def test_changing_budget_changes_plan_hash() -> None:
    """否则已批准的计划可被换上更大的预算继续执行。"""
    bigger = FIXTURE_PLAN.budget.model_copy(update={"max_tool_calls": 999})
    mutated = FIXTURE_PLAN.model_copy(update={"budget": bigger})
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability_id", "other.capability"),
        ("capability_version", "1.0.1"),
        ("policy_profile", "other.profile"),
        ("policy_revision", "policy-2026-01-01"),
    ],
)
def test_every_plan_level_field_affects_the_hash(field: str, value: str) -> None:
    mutated = FIXTURE_PLAN.model_copy(update={field: value})
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_step_reordering_changes_plan_hash(two_step_plan: ExecutionPlan) -> None:
    """ordered_steps 是有序的：调换顺序即不同计划。"""
    first, second = two_step_plan.steps
    # 反转后 s2 会先于 s1 出现，其 depends_on 必须一并清空才构造得出计划；
    # 因此对比对象是"同一批步骤的另一种合法排列"。
    reordered = two_step_plan.model_copy(
        update={"steps": (second.model_copy(update={"depends_on": ()}), first)}
    )
    assert compute_plan_hash(reordered) != compute_plan_hash(two_step_plan)


def test_changing_tool_call_args_changes_tool_call_hash() -> None:
    """准入凭证必须能证明被执行的是已准入的那个调用。"""
    mutated = FIXTURE_TOOL_CALL.model_copy(update={"typed_args": {"window_minutes": 1440}})
    assert compute_tool_call_hash(mutated) != compute_tool_call_hash(FIXTURE_TOOL_CALL)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gateway", "other"),
        ("operation", "other"),
        ("step_id", "s2"),
        ("timeout_seconds", 60.0),
        ("idempotency_key", "idem-2"),
    ],
)
def test_every_tool_call_field_affects_its_hash(field: str, value: object) -> None:
    mutated = FIXTURE_TOOL_CALL.model_copy(update={field: value})
    assert compute_tool_call_hash(mutated) != compute_tool_call_hash(FIXTURE_TOOL_CALL)


def test_resource_id_order_does_not_change_fingerprint() -> None:
    swapped = FIXTURE_TARGET.model_copy(update={"resource_ids": ("c2", "c1")})
    assert compute_target_fingerprint(swapped) == compute_target_fingerprint(FIXTURE_TARGET)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "other"),
        ("environment_id", "prod"),
        ("provider", "mysql"),
        ("resource_kind", "database"),
        ("selector_version", "2"),
    ],
)
def test_every_target_field_affects_its_fingerprint(field: str, value: str) -> None:
    mutated = FIXTURE_TARGET.model_copy(update={field: value})
    assert compute_target_fingerprint(mutated) != compute_target_fingerprint(FIXTURE_TARGET)

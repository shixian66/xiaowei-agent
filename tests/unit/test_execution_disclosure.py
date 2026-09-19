"""Pure ExecutionDisclosure projection."""

import datetime as dt

from xiaowei_agent.application.default_capabilities import SLOW_QUERY_BINDING
from xiaowei_agent.capabilities.specs import CAPABILITY_ID, CAPABILITY_VERSION, OP_COUNT, OP_LIST
from xiaowei_agent.contracts import (
    ClarificationField,
    ConfirmedSlot,
    EffectClass,
    ExecutionDisclosureDisposition,
    ExecutionPlan,
    PlanBudget,
    PlanStep,
    ReadClass,
    ResolvedTarget,
)
from xiaowei_agent.planning.disclosure import (
    DisclosureProjectionBinding,
    project_execution_disclosure,
)
from xiaowei_agent.planning.slot_verification import confirmed_text_value

_START = dt.datetime(2026, 9, 19, 10, 0, tzinfo=dt.UTC)
_END = dt.datetime(2026, 9, 19, 10, 30, tzinfo=dt.UTC)


def _slow_query_args(*, database: str = "analytics") -> dict[str, object]:
    return {
        "window_start": _START.isoformat(),
        "window_end": _END.isoformat(),
        "min_query_time_ms": 10_000,
        "row_limit": 20,
        "database": database,
        "user_name": None,
        "query_id": None,
    }


def _step(
    step_id: str = "s1",
    *,
    operation: str = OP_LIST,
    effect_class: EffectClass = EffectClass.READ,
    read_class: ReadClass | None = ReadClass.BOUNDED,
    side_effect: bool = False,
    typed_arguments: dict[str, object] | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        operation=operation,
        typed_arguments=_slow_query_args() if typed_arguments is None else typed_arguments,
        depends_on=(),
        side_effect=side_effect,
        effect_class=effect_class,
        read_class=read_class,
    )


def _plan(*steps: PlanStep, **overrides: object) -> ExecutionPlan:
    return ExecutionPlan(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        steps=steps or (_step(),),
        policy_profile="readonly.starrocks.slow_query.v1",
        policy_revision="policy-2026-09-01",
        budget=PlanBudget(max_steps=4, max_tool_calls=4, max_model_tokens=8000),
        **overrides,
    )


def _target() -> ResolvedTarget:
    return ResolvedTarget(
        tenant_id="tenant-a",
        environment_id="dev",
        provider="starrocks",
        resource_kind="cluster",
        resource_ids=("starrocks-dev-1",),
        selector_version="2",
    )


def _binding() -> DisclosureProjectionBinding:
    return DisclosureProjectionBinding(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        allowed_clarification_fields=SLOW_QUERY_BINDING.input_binding.allowed_clarification_fields,
        confirmed_slot_projector=SLOW_QUERY_BINDING.input_binding.confirmed_slot_projector,
    )


def _slot(field: ClarificationField, text: str) -> ConfirmedSlot:
    return ConfirmedSlot(field=field, value=confirmed_text_value(text))


def test_disclosure_projects_bounded_read_plan_from_plan_and_target() -> None:
    plan = _plan(
        _step("list", operation=OP_LIST),
        _step("count", operation=OP_COUNT, typed_arguments=_slow_query_args()),
    )

    disclosure = project_execution_disclosure(
        plan=plan,
        target=_target(),
        binding=_binding(),
    )

    assert disclosure.capability_id == CAPABILITY_ID
    assert disclosure.capability_version == CAPABILITY_VERSION
    assert disclosure.environment_id == "dev"
    assert disclosure.provider == "starrocks"
    assert disclosure.resource_kind == "cluster"
    assert disclosure.resource_ids == ("starrocks-dev-1",)
    assert disclosure.pure_read_only is True
    assert disclosure.plan_disposition is ExecutionDisclosureDisposition.BOUNDED_READ
    assert disclosure.read_classes == (ReadClass.BOUNDED,)
    assert disclosure.has_side_effect is False
    assert disclosure.external_target_access is True
    assert [step.operation for step in disclosure.steps] == [OP_LIST, OP_COUNT]
    assert [slot.field for slot in disclosure.confirmed_slots] == [
        ClarificationField.DATABASE,
        ClarificationField.TIME_RANGE,
    ]


def test_restricted_read_changes_plan_disposition_without_side_effect() -> None:
    plan = _plan(_step(read_class=ReadClass.RESTRICTED))

    disclosure = project_execution_disclosure(
        plan=plan,
        target=_target(),
        binding=_binding(),
    )

    assert disclosure.pure_read_only is True
    assert disclosure.plan_disposition is ExecutionDisclosureDisposition.RESTRICTED_READ
    assert disclosure.read_classes == (ReadClass.RESTRICTED,)


def test_side_effect_changes_plan_disposition() -> None:
    plan = _plan(
        _step(
            effect_class=EffectClass.MUTATE_TARGET,
            read_class=None,
            side_effect=True,
        )
    )
    binding = DisclosureProjectionBinding(
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_VERSION,
        allowed_clarification_fields=frozenset(),
        confirmed_slot_projector=None,
    )

    disclosure = project_execution_disclosure(
        plan=plan,
        target=_target(),
        binding=binding,
    )

    assert disclosure.pure_read_only is False
    assert disclosure.has_side_effect is True
    assert disclosure.plan_disposition is ExecutionDisclosureDisposition.SIDE_EFFECT
    assert disclosure.confirmed_slots == ()


def test_parent_slot_changes_are_projected_from_parent_snapshot() -> None:
    plan = _plan(_step(typed_arguments=_slow_query_args(database="ops")))
    parent_slots = (_slot(ClarificationField.DATABASE, "analytics"),)

    disclosure = project_execution_disclosure(
        plan=plan,
        target=_target(),
        binding=_binding(),
        parent_confirmed_slots=parent_slots,
    )

    assert len(disclosure.parent_changes) == 2
    by_field = {change.field: change for change in disclosure.parent_changes}
    assert by_field[ClarificationField.DATABASE].previous == parent_slots[0].value
    assert by_field[ClarificationField.DATABASE].current == _slot(
        ClarificationField.DATABASE, "ops"
    ).value
    assert by_field[ClarificationField.TIME_RANGE].previous is None
    assert by_field[ClarificationField.TIME_RANGE].current == next(
        slot.value
        for slot in disclosure.confirmed_slots
        if slot.field is ClarificationField.TIME_RANGE
    )

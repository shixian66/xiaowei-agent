"""Clarification parent subjects cannot retarget capability input binding."""

import datetime as dt
from dataclasses import replace

import pytest
from tests.fakes.admission import POLICY_SNAPSHOT
from tests.fakes.prometheus_recordings import recordings_for
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.capability_input import SlotReady
from xiaowei_agent.application.capability_runtime import (
    CapabilityBindingRegistry,
    CapabilityRuntimeBinding,
)
from xiaowei_agent.application.default_capabilities import PROMETHEUS_ALERT_BINDING
from xiaowei_agent.contracts import (
    AttemptIntent,
    Candidate,
    CapabilitySubject,
    ClarificationContext,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedSlot,
    ConfirmedTextValue,
    ExecutionPlan,
    IntentDraft,
    RequestContext,
    ResolvedTarget,
    TaskStatus,
)
from xiaowei_agent.persistence.clarification_records import ClarificationRecordCandidate
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.prometheus.slots import (
    project_prometheus_alert_confirmed_slots,
    verify_prometheus_alert_slots,
)
from xiaowei_agent.tools.alertmanager_fake import AlertmanagerRecordingAdapter
from xiaowei_agent.tools.prometheus_fake import PrometheusRecordingAdapter

pytestmark = pytest.mark.security


def _prometheus_harness() -> RuntimeHarness:
    alerts, metrics = recordings_for("golden")
    return RuntimeHarness(
        None,
        adapters={
            "alertmanager": AlertmanagerRecordingAdapter(alerts),
            "prometheus": PrometheusRecordingAdapter(metrics),
        },
        as_of=dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC),
    )


def _install_runtime_binding(
    harness: RuntimeHarness, replacement: CapabilityRuntimeBinding
) -> None:
    existing = tuple(harness.runtime._bindings._bindings.values())
    bindings = tuple(
        replacement
        if (
            binding.capability_id,
            binding.capability_version,
        )
        == (replacement.capability_id, replacement.capability_version)
        else binding
        for binding in existing
    )
    harness.runtime._bindings = CapabilityBindingRegistry(
        snapshot=harness.runtime._snapshot,
        policy_snapshot=POLICY_SNAPSHOT,
        bindings=bindings,
    )


async def _dispatch(harness: RuntimeHarness, *, owner: str = "worker-1") -> object:
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=harness.task_id,
            intent=AttemptIntent.DISPATCH,
            owner=owner,
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert attempt.grant is not None
    assert attempt.submission is not None
    return await harness.runtime.execute_task(
        grant=attempt.grant,
        submission=attempt.submission,
    )


async def test_child_rejects_incompatible_parent_capability_subject() -> None:
    harness = RuntimeHarness(GOLDEN)
    parent_submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="parent-incompatible",
    )
    parent = await harness.runtime.submit_task(submission=parent_submission)
    parent_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=parent.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=parent_submission.context.trace_id,
        )
    )
    assert parent_attempt.grant is not None
    slot = ConfirmedSlot(
        field=ClarificationField.ALERT_NAME,
        value=ConfirmedTextValue(kind="text", text="HostHighCpu"),
    )
    await harness.clarification_records.save(
        grant=parent_attempt.grant,
        candidate=ClarificationRecordCandidate(
            subject=CapabilitySubject(
                kind="capability",
                capability_id="asset.inventory.lookup",
                capability_version="1.0.0",
                operation="lookup_asset",
                input_schema_ref="input.asset.lookup.v1",
                confirmed_slots=(slot,),
            ),
            reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
            missing_fields=(ClarificationField.INSTANCE,),
            confirmed_slots=(slot,),
        ),
    )
    transitioned = await harness.store.transition(
        command=TransitionCommand(
            task_id=parent.task_id,
            expected_version=parent_attempt.winner.version,
            to_status=TaskStatus.CLARIFICATION_REQUIRED,
            fencing_token=parent_attempt.grant.fencing_token,
        )
    )
    assert transitioned.applied

    child_submission = harness.submission(
        "查告警 HostHighCpu 在 node-1.example.com:9100 的证据",
        idempotency_key="child-incompatible",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id
    child_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=child.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-2",
            ttl_seconds=60,
            trace_id=child_submission.context.trace_id,
        )
    )
    assert child_attempt.grant is not None

    outcome = await harness.runtime.execute_task(
        grant=child_attempt.grant,
        submission=child_submission,
    )

    assert outcome.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0


async def test_child_rejects_parent_input_schema_ref_drift_before_gateway() -> None:
    harness = _prometheus_harness()
    parent_submission = harness.submission(
        "查告警 HostHighCpu 的证据",
        idempotency_key="parent-schema-drift",
    )
    parent = await harness.runtime.submit_task(submission=parent_submission)
    harness.task_id = parent.task_id
    parent_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=parent.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=parent_submission.context.trace_id,
        )
    )
    assert parent_attempt.grant is not None
    slot = ConfirmedSlot(
        field=ClarificationField.ALERT_NAME,
        value=ConfirmedTextValue(kind="text", text="HostHighCpu"),
    )
    await harness.clarification_records.save(
        grant=parent_attempt.grant,
        candidate=ClarificationRecordCandidate(
            subject=CapabilitySubject(
                kind="capability",
                capability_id=PROMETHEUS_ALERT_BINDING.capability_id,
                capability_version=PROMETHEUS_ALERT_BINDING.capability_version,
                operation=PROMETHEUS_ALERT_BINDING.entry_operation,
                input_schema_ref="input.asset.lookup.v1",
                confirmed_slots=(slot,),
            ),
            reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
            missing_fields=(ClarificationField.INSTANCE,),
            confirmed_slots=(slot,),
        ),
    )
    transitioned = await harness.store.transition(
        command=TransitionCommand(
            task_id=parent.task_id,
            expected_version=parent_attempt.winner.version,
            to_status=TaskStatus.CLARIFICATION_REQUIRED,
            fencing_token=parent_attempt.grant.fencing_token,
        )
    )
    assert transitioned.applied

    child_submission = harness.submission(
        "node-1.example.com:9100",
        idempotency_key="child-schema-drift",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id

    outcome = await _dispatch(harness, owner="worker-2")

    assert outcome.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0
    assert harness.adapters["alertmanager"].call_count == 0
    assert harness.adapters["prometheus"].call_count == 0


async def test_runtime_rejects_projector_that_drops_confirmed_slots_before_gateway() -> None:
    harness = _prometheus_harness()
    projector = PROMETHEUS_ALERT_BINDING.input_binding.confirmed_slot_projector
    assert projector is not None

    def dropping_projector(
        *, plan: ExecutionPlan, target: ResolvedTarget
    ) -> tuple[ConfirmedSlot, ...]:
        projected = project_prometheus_alert_confirmed_slots(plan=plan, target=target)
        return tuple(
            slot for slot in projected if slot.field is not ClarificationField.INSTANCE
        )

    _ = projector
    _install_runtime_binding(
        harness,
        replace(
            PROMETHEUS_ALERT_BINDING,
            input_binding=replace(
                PROMETHEUS_ALERT_BINDING.input_binding,
                confirmed_slot_projector=dropping_projector,
            ),
        ),
    )
    parent = await harness.runtime.submit_task(
        submission=harness.submission(
            "查告警 HostHighCpu 的证据",
            idempotency_key="parent-projector-drift",
        )
    )
    harness.task_id = parent.task_id
    parent_outcome = await _dispatch(harness, owner="worker-1")
    assert parent_outcome.status is TaskStatus.CLARIFICATION_REQUIRED

    child_submission = harness.submission(
        "node-1.example.com:9100",
        idempotency_key="child-projector-drift",
    ).model_copy(update={"clarification_parent_task_id": parent.task_id})
    child = await harness.store.create_clarification_child(
        submission=child_submission,
        authenticated_channel_owner=harness.context.actor,
    )
    harness.task_id = child.task_id

    outcome = await _dispatch(harness, owner="worker-2")

    assert outcome.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0
    assert harness.adapters["alertmanager"].call_count == 0
    assert harness.adapters["prometheus"].call_count == 0


class _PrometheusAlertParamsSubclass(PrometheusAlertParams):
    pass


async def test_runtime_rejects_slot_ready_params_subclass_before_gateway() -> None:
    harness = _prometheus_harness()

    def subclass_verifier(
        *,
        candidate: Candidate,
        draft: IntentDraft,
        context: RequestContext,
        as_of: dt.datetime,
        user_text: str,
        clarification: ClarificationContext | None = None,
    ) -> object:
        result = verify_prometheus_alert_slots(
            candidate=candidate,
            draft=draft,
            context=context,
            as_of=as_of,
            user_text=user_text,
            clarification=clarification,
        )
        if not isinstance(result, SlotReady):
            return result
        return SlotReady(
            params=_PrometheusAlertParamsSubclass.model_validate(
                result.params.model_dump(mode="python")
            ),
            confirmed_slots=result.confirmed_slots,
        )

    _install_runtime_binding(
        harness,
        replace(
            PROMETHEUS_ALERT_BINDING,
            input_binding=replace(
                PROMETHEUS_ALERT_BINDING.input_binding,
                slot_verifier=subclass_verifier,
            ),
        ),
    )

    payload = await harness.handle(
        "查告警 HostHighCpu 在 node-1.example.com:9100 的证据",
        idempotency_key="params-subclass",
    )

    assert payload.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0
    assert harness.adapters["alertmanager"].call_count == 0
    assert harness.adapters["prometheus"].call_count == 0

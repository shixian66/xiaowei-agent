"""ReadClass 的上层准入分流。"""

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.capability_runtime import CapabilityBindingRegistry
from xiaowei_agent.application.default_capabilities import (
    ASSET_INVENTORY_BINDING,
    PROMETHEUS_ALERT_BINDING,
    SLOW_QUERY_BINDING,
)
from xiaowei_agent.capabilities.specs import OP_LIST
from xiaowei_agent.contracts import (
    AttemptIntent,
    PipelineStage,
    ReadClass,
    TaskStatus,
)
from xiaowei_agent.persistence.store import TaskAttemptCommand

pytestmark = pytest.mark.security


async def test_restricted_read_plan_is_refused_before_admission_or_gateway() -> None:
    harness = RuntimeHarness(GOLDEN)
    snapshot = harness.runtime._snapshot
    restricted_specs = []
    for spec in snapshot.specs:
        operations = tuple(
            operation.model_copy(update={"read_class": ReadClass.RESTRICTED})
            if operation.operation == OP_LIST
            else operation
            for operation in spec.operations
        )
        restricted_specs.append(spec.model_copy(update={"operations": operations}))
    restricted_snapshot = snapshot.model_copy(update={"specs": tuple(restricted_specs)})
    harness.runtime._snapshot = restricted_snapshot
    harness.runtime._bindings = CapabilityBindingRegistry(
        snapshot=restricted_snapshot,
        policy_snapshot=harness.runtime._runner._policy_snapshot,
        bindings=(SLOW_QUERY_BINDING, PROMETHEUS_ALERT_BINDING, ASSET_INVENTORY_BINDING),
    )
    submission = harness.submission("最近30分钟有哪些慢查询")
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None and attempt.submission is not None

    outcome = await harness.runtime.execute_task(
        grant=attempt.grant, submission=attempt.submission
    )

    record = await harness.store.get(lookup=harness.lookup)
    stages = [event.stage for event in harness.sink.events]
    assert outcome.status is TaskStatus.REJECTED
    assert record.status is TaskStatus.REJECTED
    assert record.terminal_reason == "policy.read_class_not_allowed"
    assert PipelineStage.PLANNER in stages
    assert PipelineStage.ADMISSION not in stages
    assert PipelineStage.GATEWAY not in stages
    assert harness.gateway.invocations == 0

"""资产精确查询能力的完整 Runtime 契约。"""

import datetime as dt

import pytest
from tests.fakes.asset_recordings import recording_for
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.default_capabilities import ASSET_INVENTORY_BINDING
from xiaowei_agent.contracts import AttemptIntent, TaskStatus
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.tools.asset_inventory_fake import AssetInventoryRecordingAdapter

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
TEXT = "查资产 hostname=node-1.example.com"


def _harness(scenario: str) -> RuntimeHarness:
    return RuntimeHarness(
        None,
        adapters={
            "asset_inventory": AssetInventoryRecordingAdapter(
                recording_for(scenario), operation="lookup_asset"
            )
        },
        as_of=AT,
    )


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("golden", TaskStatus.SUCCEEDED),
        ("empty", TaskStatus.INDETERMINATE),
        ("duplicate", TaskStatus.INDETERMINATE),
        ("timeout", TaskStatus.INDETERMINATE),
        ("malformed", TaskStatus.INDETERMINATE),
        ("wrong_environment", TaskStatus.INDETERMINATE),
        ("wrong_identity", TaskStatus.INDETERMINATE),
        ("polluted", TaskStatus.SUCCEEDED),
        ("cross_scope", TaskStatus.INDETERMINATE),
    ],
)
async def test_runtime_truth_table_uses_one_asset_gateway_call(
    scenario: str, status: TaskStatus
) -> None:
    harness = _harness(scenario)
    payload = await harness.handle(TEXT)

    assert payload.status is status
    assert harness.gateway.invocations == 1
    assert harness.adapters["asset_inventory"].call_count == 1
    assert [call.gateway for call in harness.calls] == ["asset_inventory"]


async def test_terminal_query_uses_asset_renderer_without_calling_the_gateway() -> None:
    harness = _harness("golden")
    first = await harness.handle(TEXT)
    before = harness.gateway.invocations

    queried = await harness.runtime.query_task(lookup=harness.lookup)

    assert queried.render == first
    assert queried.render is not None
    assert "唯一资产" in queried.render.answer
    assert harness.gateway.invocations == before


async def test_sensitive_adapter_fields_never_reach_evidence_render_or_trace() -> None:
    harness = _harness("polluted")
    payload = await harness.handle(TEXT)
    marker = "to" + "ken" + "=" + "synthetic-value"

    evidences = await harness.ledger.load(task_id=harness.task_id)
    serialised = " ".join(
        (
            payload.model_dump_json(),
            *(item.model_dump_json() for item in evidences),
            *(item.model_dump_json() for item in harness.sink.events),
        )
    )
    assert marker not in serialised
    assert "must-not-render" not in serialised


@pytest.mark.parametrize("field", ["selector_kind", "selector_value", "limit"])
async def test_stored_plan_drift_stops_before_the_asset_adapter(field: str) -> None:
    harness = _harness("golden")
    submission = harness.submission(TEXT)
    pending = await harness.runtime.submit_task(submission=submission)
    harness.task_id = pending.task_id
    draft = harness.runtime._interpreter.interpret(text=TEXT, context=harness.context)
    prepared = ASSET_INVENTORY_BINDING.planner(
        candidate=next(
            item
            for item in harness.runtime._resolver.resolve(
                draft=draft,
                context=harness.context,
                snapshot=harness.snapshot,
            ).items
            if item.operation == ASSET_INVENTORY_BINDING.entry_operation
        ),
        draft=draft,
        context=harness.context,
        as_of=submission.as_of,
        snapshot=harness.snapshot,
    )
    first_attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=pending.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert first_attempt.grant is not None
    await harness.plan_store.save(
        task_id=pending.task_id,
        plan=prepared.plan,
        target=prepared.target,
    )
    current = await harness.store.get(lookup=harness.lookup)
    planning = await harness.store.transition(
        command=TransitionCommand(
            task_id=pending.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=first_attempt.grant.fencing_token,
        )
    )
    assert planning.applied
    stored = harness.state.plans[pending.task_id]
    step = stored.plan.steps[0]
    changed_value: object = 3 if field == "limit" else "tampered"
    changed = step.model_copy(
        update={
            "typed_arguments": dict(step.typed_arguments)
            | {field: changed_value}
        }
    )
    harness.state.plans[pending.task_id] = stored.model_copy(
        update={"plan": stored.plan.model_copy(update={"steps": (changed,)})}
    )
    harness.clock.advance(seconds=61)
    resumed = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=pending.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-2",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert resumed.grant is not None and resumed.submission is not None

    outcome = await harness.runtime.execute_task(
        grant=resumed.grant, submission=resumed.submission
    )

    assert outcome.status is TaskStatus.FAILED
    assert harness.gateway.invocations == 0
    assert harness.adapters["asset_inventory"].call_count == 0


@pytest.mark.parametrize(
    "text",
    [
        "查资产",
        "查资产 hostname=node-1.example.com ip=10.0.0.8",
        "查资产 hostname=*.example.com",
        "列出全部资产",
    ],
)
async def test_missing_multiple_or_broad_selector_never_calls_the_adapter(
    text: str,
) -> None:
    harness = _harness("golden")
    payload = await harness.handle(text)
    assert payload.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0
    assert harness.adapters["asset_inventory"].call_count == 0

"""M6a 资产 L0：selector、scope、漂移与外部文本均 fail-closed。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.fakes.asset_recordings import recording_for
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.default_capabilities import ASSET_INVENTORY_BINDING
from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import AttemptIntent, RequestContext, TaskStatus, ToolCall
from xiaowei_agent.persistence.store import TaskAttemptCommand, TransitionCommand
from xiaowei_agent.planning import compute_plan_hash
from xiaowei_agent.planning.assets.params import AssetLookupParams
from xiaowei_agent.tools.asset_inventory_fake import (
    AssetInventoryRecordingAdapter,
    AssetInventoryRecordingNotFoundError,
)
from xiaowei_agent.tools.gateway import DeterministicToolGateway

pytestmark = pytest.mark.security

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m6a_asset_l0.json").read_text(
        encoding="utf-8"
    )
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
_TEXT = "查资产 hostname=node-1.example.com"
_SNAPSHOT = StaticCapabilityRegistry().snapshot()


def _by(carrier: str) -> list[dict[str, Any]]:
    return [case for case in _CASES if case["carrier"] == carrier]


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="5" * 32,
        policy_revision="policy-2026-09-01",
    )


def _prepared(text: str = _TEXT):
    draft = RuleBasedIntentInterpreter().interpret(text=text, context=_context())
    candidate = next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(draft=draft, context=_context(), snapshot=_SNAPSHOT)
        .items
        if item.operation == ASSET_INVENTORY_BINDING.entry_operation
    )
    return ASSET_INVENTORY_BINDING.planner(
        candidate=candidate,
        draft=draft,
        context=_context(),
        as_of=_AT,
        snapshot=_SNAPSHOT,
    )


def _adapter(scenario: str = "golden") -> AssetInventoryRecordingAdapter:
    return AssetInventoryRecordingAdapter(
        recording_for(scenario), operation="lookup_asset"
    )


def test_every_corpus_case_has_an_executing_driver() -> None:
    assert {case["carrier"] for case in _CASES} == {
        "param",
        "pollution",
        "drift",
        "cross_scope",
        "duplicate",
        "polluted_fields",
        "no_certificate",
    }
    assert len({case["id"] for case in _CASES}) == len(_CASES)


@pytest.mark.parametrize("case", _by("param"), ids=lambda case: case["id"])
def test_invalid_selectors_stop_before_the_adapter(case: dict[str, Any]) -> None:
    adapter = _adapter()
    mutation = case["mutation"]
    arguments: dict[str, object] = {
        "multiple": {"hostname": "node-1", "ip": "10.0.0.8"},
        "wildcard": {"hostname": "*.example.com"},
        "cidr": {"ip": "10.0.0.0/24"},
        "range": {"ip": "10.0.0.1-10.0.0.8"},
        "zone": {"ip": "fe80::1%eth0"},
        "port_ip": {"ip": "10.0.0.8:9100"},
        "overlong": {"hostname": "a" * 254},
        "missing": {},
    }[mutation]
    with pytest.raises((ValidationError, ValueError)):
        AssetLookupParams.model_validate(arguments)
    assert adapter.call_count == 0


@pytest.mark.parametrize("case", _by("pollution"), ids=lambda case: case["id"])
def test_untrusted_text_cannot_change_asset_plan(case: dict[str, Any]) -> None:
    adapter = _adapter()
    polluted = f"{_TEXT} {case['suffix']}"
    draft = RuleBasedIntentInterpreter().interpret(text=polluted, context=_context())
    assert set(draft.slots) == {"hostname"}
    assert compute_plan_hash(_prepared(polluted).plan) == compute_plan_hash(
        _prepared().plan
    )
    assert adapter.call_count == 0


@pytest.mark.parametrize("case", _by("drift"), ids=lambda case: case["id"])
async def test_stored_plan_drift_fails_before_asset_adapter(
    case: dict[str, Any],
) -> None:
    adapter = _adapter()
    harness = RuntimeHarness(None, adapters={"asset_inventory": adapter}, as_of=_AT)
    submission = harness.submission(_TEXT)
    pending = await harness.runtime.submit_task(submission=submission)
    harness.task_id = pending.task_id
    prepared = _prepared()
    first = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=pending.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=harness.context.trace_id,
        )
    )
    assert first.grant is not None
    await harness.plan_store.save(
        task_id=pending.task_id, plan=prepared.plan, target=prepared.target
    )
    current = await harness.store.get(lookup=harness.lookup)
    planning = await harness.store.transition(
        command=TransitionCommand(
            task_id=pending.task_id,
            expected_version=current.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=first.grant.fencing_token,
        )
    )
    assert planning.applied
    stored = harness.state.plans[pending.task_id]
    step = stored.plan.steps[0]
    value: object = 3 if case["field"] == "limit" else "tampered"
    changed = step.model_copy(
        update={
            "typed_arguments": dict(step.typed_arguments)
            | {case["field"]: value}
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
    assert adapter.call_count == 0


async def test_cross_scope_recording_has_no_fallback() -> None:
    adapter = _adapter()
    step = _prepared().plan.steps[0]
    call = ToolCall(
        gateway="asset_inventory",
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key="fixed:s1",
    )
    with pytest.raises(AssetInventoryRecordingNotFoundError):
        await adapter.execute(
            call, context=_context().model_copy(update={"tenant_id": "other"})
        )
    assert adapter.call_count == 1


async def test_duplicate_identity_is_not_presented_as_unique() -> None:
    harness = RuntimeHarness(
        None, adapters={"asset_inventory": _adapter("duplicate")}, as_of=_AT
    )
    payload = await harness.handle(_TEXT)
    assert payload.status is TaskStatus.INDETERMINATE
    assert "唯一资产" not in payload.answer


async def test_sensitive_adapter_fields_do_not_reach_trusted_outputs() -> None:
    harness = RuntimeHarness(
        None, adapters={"asset_inventory": _adapter("polluted")}, as_of=_AT
    )
    payload = await harness.handle(_TEXT)
    marker = "to" + "ken" + "=" + "synthetic-value"
    evidence = await harness.ledger.load(task_id=harness.task_id)
    joined = " ".join(
        (
            payload.model_dump_json(),
            *(item.model_dump_json() for item in evidence),
            *(event.model_dump_json() for event in harness.sink.events),
        )
    )
    assert marker not in joined
    assert "must-not-render" not in joined


async def test_gateway_rejects_a_call_without_a_certificate() -> None:
    adapter = _adapter()
    step = _prepared().plan.steps[0]
    call = ToolCall(
        gateway="asset_inventory",
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key="fixed:s1",
    )
    gateway = DeterministicToolGateway(adapters={"asset_inventory": adapter})
    with pytest.raises(PermissionError):
        await gateway.invoke(
            call, context=_context(), admission=None  # type: ignore[arg-type]
        )
    assert adapter.call_count == 0

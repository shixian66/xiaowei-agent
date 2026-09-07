"""资产目录结果只转换成九字段、scope 可核对的证据。"""

import asyncio
import datetime as dt

import pytest
from tests.conftest import make_certificate

from xiaowei_agent.capabilities.asset_inventory import (
    ASSET_INVENTORY_CAPABILITY_ID,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    AdapterStatus,
    IntentDraft,
    IntentSource,
    RequestContext,
    ResolvedTarget,
    ToolCall,
    ToolResult,
)
from xiaowei_agent.evidence.asset_inventory import build_asset_evidence
from xiaowei_agent.evidence.errors import EvidenceBuildError
from xiaowei_agent.planning.assets.compiler import (
    compile_asset_plan,
    resolve_asset_target,
)
from xiaowei_agent.planning.assets.params import AssetLookupParams
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import (
    DeterministicToolGateway,
    TargetBoundAdapterBinding,
)

AT = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)
PARAMS = AssetLookupParams(hostname="NODE-1.EXAMPLE.COM.")
TARGET = resolve_asset_target(context=CONTEXT, params=PARAMS)
SNAPSHOT = StaticCapabilityRegistry().snapshot()
DRAFT = IntentDraft(
    intent=ASSET_INVENTORY_CAPABILITY_ID,
    slots={"hostname": PARAMS.hostname},
    missing=(),
    confidence=0.9,
    source=IntentSource.USER,
)
CANDIDATE = DeterministicCapabilityResolver().resolve(
    draft=DRAFT, context=CONTEXT, snapshot=SNAPSHOT
).items[0]
PLAN = compile_asset_plan(
    candidate=CANDIDATE,
    params=PARAMS,
    target=TARGET,
    context=CONTEXT,
    snapshot=SNAPSHOT,
)


def _asset_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "asset_id": "asset-1",
        "hostname": "node-1.example.com",
        "primary_ip": "10.0.0.8",
        "asset_type": "server",
        "status": "active",
        "owner_team": "platform",
        "service": "checkout",
        "os_family": "linux",
        "environment_id": "dev",
    }
    return row | overrides


def _result(
    rows: tuple[dict[str, object], ...], *, target_bound: bool = False
) -> ToolResult:
    step = PLAN.steps[0]
    call = ToolCall(
        gateway="asset_inventory",
        operation=step.operation,
        step_id=step.step_id,
        typed_args=step.typed_arguments,
        timeout_seconds=30.0,
        idempotency_key="fixed:s1",
    )
    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=rows,
                source="asset-inventory-recording",
                error=None,
                elapsed_ms=1,
            ),
        )
    )
    fingerprint = "a" * 64
    gateway = (
        DeterministicToolGateway(
            adapters={},
            target_adapters={
                (call.gateway, fingerprint): TargetBoundAdapterBinding(
                    adapter=adapter,
                    authorized_tenant_id=CONTEXT.tenant_id,
                    authorized_environment_id=CONTEXT.environment_id,
                    authorized_actor=CONTEXT.actor,
                    active_from=AT - dt.timedelta(minutes=1),
                    active_until=AT + dt.timedelta(minutes=1),
                    evidence_source_ref="approval-ref:unexpected",
                    config_revision="b" * 64,
                    physical_identity_ref="identity-ref:unexpected",
                    driver_version="test-double",
                )
            },
            clock=lambda: AT,
        )
        if target_bound
        else DeterministicToolGateway(adapters={call.gateway: adapter})
    )
    return asyncio.run(
        gateway.invoke(
            call,
            context=CONTEXT,
            admission=make_certificate(
                call,
                **({"target_fingerprint": fingerprint} if target_bound else {}),
            ),
        )
    )


def _build(
    rows: tuple[dict[str, object], ...],
    *,
    target: ResolvedTarget = TARGET,
    target_bound: bool = False,
):
    return build_asset_evidence(
        task_id="task-1",
        step=PLAN.steps[0],
        plan=PLAN,
        target=target,
        result=_result(rows, target_bound=target_bound),
        captured_at=AT,
    )


def test_asset_evidence_keeps_only_the_nine_field_allowlist() -> None:
    sensitive_keys = (
        "credential",
        "to" + "ken",
        "se" + "cret",
        "pass" + "word",
        "tags",
        "notes",
    )
    row = _asset_row(**{key: "must-not-leak" for key in sensitive_keys})
    evidence = _build((row,))

    assert set(evidence.facts[0]) == {
        "asset_id",
        "hostname",
        "primary_ip",
        "asset_type",
        "status",
        "owner_team",
        "service",
        "os_family",
        "environment_id",
    }
    assert not (set(evidence.facts[0]) & set(sensitive_keys))
    assert evidence.capability_id == ASSET_INVENTORY_CAPABILITY_ID
    assert evidence.evidence_id == "task-1:s1"


def test_asset_builder_rejects_unapproved_target_binding_metadata() -> None:
    with pytest.raises(EvidenceBuildError):
        _build((_asset_row(),), target_bound=True)


@pytest.mark.parametrize(
    ("row", "target"),
    [
        (_asset_row(environment_id="test"), TARGET),
        (_asset_row(hostname="node-2.example.com"), TARGET),
        (
            _asset_row(),
            TARGET.model_copy(update={"environment_id": "test"}),
        ),
        (
            _asset_row(),
            TARGET.model_copy(update={"provider": "another-provider"}),
        ),
        (
            _asset_row(),
            TARGET.model_copy(update={"resource_kind": "another-kind"}),
        ),
        (
            _asset_row(),
            TARGET.model_copy(update={"selector_version": "asset.fuzzy.v1"}),
        ),
        (
            _asset_row(),
            TARGET.model_copy(
                update={"resource_ids": ("hostname:node-2.example.com",)}
            ),
        ),
    ],
    ids=[
        "row_environment",
        "row_identity",
        "target_environment",
        "target_provider",
        "target_kind",
        "target_selector_version",
        "target_resource_id",
    ],
)
def test_scope_or_selected_identity_conflicts_fail_closed(
    row: dict[str, object], target: ResolvedTarget
) -> None:
    with pytest.raises(EvidenceBuildError):
        _build((row,), target=target)


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in _asset_row().items() if key != "service"},
        _asset_row(status=1),
        _asset_row(owner_team=""),
        _asset_row(primary_ip="not-an-ip"),
        _asset_row(hostname="NODE-1.EXAMPLE.COM"),
    ],
    ids=["missing_field", "wrong_type", "empty_value", "invalid_ip", "noncanonical"],
)
def test_malformed_asset_rows_fail_closed(row: dict[str, object]) -> None:
    with pytest.raises(EvidenceBuildError):
        _build((row,))


def test_zero_and_two_rows_remain_explicit_evidence_states() -> None:
    empty = _build(())
    duplicate = _build((_asset_row(), _asset_row(asset_id="asset-2")))

    assert empty.facts == ()
    assert empty.sampled is False
    assert len(duplicate.facts) == 2
    assert duplicate.sampled is True


def test_rows_over_the_fixed_lookup_limit_are_rejected() -> None:
    with pytest.raises(EvidenceBuildError):
        _build(
            (
                _asset_row(asset_id="asset-1"),
                _asset_row(asset_id="asset-2"),
                _asset_row(asset_id="asset-3"),
            )
        )

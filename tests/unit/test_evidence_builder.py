"""证据构造：把一次工具调用的结果包成可审计的事实。"""

import datetime as dt

import pytest
from tests.fakes.admission import PARAMS, REGISTRY_SNAPSHOT, TARGET, slow_query_plan

from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE as SURFACE
from xiaowei_agent.contracts import (
    AdapterStatus,
    ExternalSource,
    RequestContext,
    ResolvedTarget,
    ToolResult,
)
from xiaowei_agent.evidence.builder import (
    SlowQueryEvidencePolicy,
    build_evidence,
    evidence_id,
)
from xiaowei_agent.evidence.errors import EvidenceBuildError
from xiaowei_agent.planning import compute_target_fingerprint
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
_TASK = "task-1"

_ROW = {
    "queryId": "q1",
    "timestamp": "2026-09-02 11:40:00",
    "queryTime": 12000,
    "db": "sales",
    "user": "app_user_1",
    "state": "FINISHED",
    "errorCode": "",
}


def _result(rows: tuple[dict[str, object], ...]) -> ToolResult:
    """经**真实 Gateway** 取得 ToolResult。

    不给 tools/ 加一个"测试用构造器"：那会把 Gateway 的签发凭据暴露到签发模块之外，
    而 test_only_the_issuing_module_references_a_construction_witness 正是禁止这件事的。
    走真实路径既不破边界，也顺带证明了证据构造消费的确实是 Gateway 归一化后的结果。
    """
    import asyncio

    from tests.conftest import make_certificate
    from tests.fakes.admission import CONTEXT, slow_query_call

    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=rows,
                source="starrocks-fake",
                error=None,
                elapsed_ms=3,
            ),
        )
    )
    gateway = DeterministicToolGateway(adapters={"starrocks": adapter})
    call = slow_query_call()
    return asyncio.run(
        gateway.invoke(call, context=CONTEXT, admission=make_certificate(call))
    )


def _generic_gateway_failure_result(*, timeout: bool) -> ToolResult:
    import asyncio

    from tests.conftest import make_certificate
    from tests.fakes.admission import CONTEXT, slow_query_call

    class _FailingAdapter:
        async def execute(self, call, *, context):
            del call, context
            if timeout:
                await asyncio.sleep(0.02)
                raise AssertionError("wait_for should cancel the adapter")
            raise RuntimeError("synthetic upstream failure")

    call = slow_query_call(timeout_seconds=0.001 if timeout else 30.0)
    return asyncio.run(
        DeterministicToolGateway(adapters={"starrocks": _FailingAdapter()}).invoke(
            call,
            context=CONTEXT,
            admission=make_certificate(call),
        )
    )


def _build(rows: tuple[dict[str, object], ...] = (_ROW,)) -> object:
    plan = slow_query_plan()
    return build_evidence(
        task_id=_TASK,
        step=plan.steps[0],
        plan=plan,
        target=TARGET,
        result=_result(rows),
        surface=SURFACE,
        params=PARAMS,
        captured_at=_AT,
    )


def test_evidence_id_is_deterministic_and_addressable() -> None:
    """证据 id 同时是 ledger 的键与 TaskOutcome.evidence_refs 的元素。"""
    assert evidence_id(task_id=_TASK, step_id="s1") == evidence_id(
        task_id=_TASK, step_id="s1"
    )
    assert evidence_id(task_id=_TASK, step_id="s1") != evidence_id(
        task_id=_TASK, step_id="s2"
    )
    assert _build().evidence_id == evidence_id(task_id=_TASK, step_id="s1")


def test_capability_identity_comes_from_the_plan_not_the_adapter() -> None:
    plan = slow_query_plan()
    envelope = _build()
    assert envelope.capability_id == plan.capability_id
    assert envelope.capability_version == plan.capability_version


def test_evidence_builder_drops_columns_outside_the_whitelist() -> None:
    """adapter 多返回的列一律丢弃。

    Gateway 归一化过的结果仍可能含未声明列——归一化管的是**形状**，不是**字段集**。
    """
    row = {**_ROW, "stmt": "SELECT 1", "clientIp": "10.0.0.1", "secretish": "x"}
    facts = _build((row,)).facts
    assert set(facts[0]) <= set(SURFACE.allowed_columns)
    assert "stmt" not in facts[0]
    assert "clientIp" not in facts[0]


def test_declared_columns_survive_the_filter() -> None:
    """反例配对：过滤不得把声明列一起丢掉，否则证据永远是空的。"""
    facts = _build().facts
    assert facts[0]["queryId"] == "q1"
    assert facts[0]["queryTime"] == 12000


def test_evidence_marks_sampling_when_the_row_cap_is_hit() -> None:
    rows = tuple({**_ROW, "queryId": f"q{i}"} for i in range(PARAMS.row_limit))
    assert _build(rows).sampled is True


def test_evidence_is_not_sampled_below_the_cap() -> None:
    assert _build().sampled is False


def test_evidence_limitations_contain_the_scope_and_the_window() -> None:
    limitations = " ".join(_build().limitations)
    assert "2026-09-02 11:30:00" in limitations
    assert "2026-09-02 12:00:00" in limitations
    assert str(PARAMS.row_limit) in limitations
    assert str(PARAMS.min_query_time_ms) in limitations


def test_evidence_never_contains_sql_text() -> None:
    dumped = _build().model_dump_json()
    assert "SELECT" not in dumped
    assert "starrocks_audit_tbl__" not in dumped


def test_evidence_is_always_readonly() -> None:
    assert _build().readonly is True


def test_source_kind_is_tool() -> None:
    envelope = _build()
    assert envelope.source_kind is ExternalSource.TOOL
    assert envelope.source == "starrocks-fake"


def test_empty_result_produces_empty_facts_not_a_missing_envelope() -> None:
    """空证据必须是"一份说明了限制的空证据"，不是没有证据。"""
    envelope = _build(())
    assert envelope.facts == ()
    assert envelope.sampled is False
    assert envelope.limitations


@pytest.mark.parametrize(
    ("timeout", "expected_limitation"),
    [
        (False, "adapter raised an unexpected exception"),
        (True, "adapter timed out"),
    ],
)
def test_generic_gateway_failure_remains_tool_result_evidence(
    timeout: bool,
    expected_limitation: str,
) -> None:
    """固定 gateway 失败文案不是 target metadata，不能破坏错误归因。"""
    plan = slow_query_plan()
    result = _generic_gateway_failure_result(timeout=timeout)
    assert result.limitations == (expected_limitation,)
    envelope = build_evidence(
        task_id=_TASK,
        step=plan.steps[0],
        plan=plan,
        target=TARGET,
        result=result,
        surface=SURFACE,
        params=PARAMS,
        captured_at=_AT,
    )

    assert envelope.facts == ()
    assert envelope.source == "starrocks"


def test_captured_at_is_injected_not_read_from_the_clock() -> None:
    assert _build().captured_at == _AT


def test_naive_captured_at_is_rejected() -> None:
    from pydantic import ValidationError

    plan = slow_query_plan()
    with pytest.raises(ValidationError):
        build_evidence(
            task_id=_TASK,
            step=plan.steps[0],
            plan=plan,
            target=TARGET,
            result=_result((_ROW,)),
            surface=SURFACE,
            params=PARAMS,
            captured_at=dt.datetime(2026, 9, 2, 12, 0),
        )


def test_builder_requires_the_admitted_target() -> None:
    """M6b 后 target 是真实证据归属核对的必需输入。"""
    import inspect

    assert "target" in inspect.signature(build_evidence).parameters


def test_snapshot_is_not_needed_to_build_evidence() -> None:
    import inspect

    assert "snapshot" not in inspect.signature(build_evidence).parameters
    assert REGISTRY_SNAPSHOT is not None


_LIVE_TARGET = ResolvedTarget(
    tenant_id="dev-local",
    environment_id="test",
    provider="starrocks",
    resource_kind="cluster",
    resource_ids=("approved-test-cluster",),
    selector_version="2",
)
_LIVE_CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="m6b-operator",
    environment_id="test",
    trace_id="1" * 32,
    policy_revision="policy-2026-09-01",
)
_LIVE_POLICY = SlowQueryEvidencePolicy(
    approved_target=_LIVE_TARGET,
    target_fingerprint=compute_target_fingerprint(_LIVE_TARGET),
    evidence_source_ref="approval-ref:m6b-1",
    config_revision="a" * 64,
    physical_identity_ref="identity-ref:test-cluster",
    driver_version="1.2.0",
    redaction_ref=None,
)


def _live_result(
    rows: tuple[dict[str, object], ...] = (_ROW,),
    *,
    source_ref: str = _LIVE_POLICY.evidence_source_ref,
    status: AdapterStatus = AdapterStatus.OK,
) -> ToolResult:
    import asyncio

    from tests.conftest import make_certificate
    from tests.fakes.admission import slow_query_call

    from xiaowei_agent.tools.gateway import TargetBoundAdapterBinding

    adapter = RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=status,
                payload=rows,
                source="untrusted-adapter-source",
                error=None,
                elapsed_ms=3,
            ),
        )
    )
    call = slow_query_call()
    fingerprint = compute_target_fingerprint(_LIVE_TARGET)
    binding = TargetBoundAdapterBinding(
        adapter=adapter,
        authorized_tenant_id=_LIVE_CONTEXT.tenant_id,
        authorized_environment_id=_LIVE_CONTEXT.environment_id,
        authorized_actor=_LIVE_CONTEXT.actor,
        active_from=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        active_until=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
        evidence_source_ref=source_ref,
        config_revision=_LIVE_POLICY.config_revision,
        physical_identity_ref=_LIVE_POLICY.physical_identity_ref,
        driver_version=_LIVE_POLICY.driver_version,
    )
    gateway = DeterministicToolGateway(
        adapters={},
        target_adapters={("starrocks", fingerprint): binding},
        clock=lambda: _AT,
    )
    return asyncio.run(
        gateway.invoke(
            call,
            context=_LIVE_CONTEXT,
            admission=make_certificate(call, target_fingerprint=fingerprint),
        )
    )


def test_generic_slow_query_evidence_rejects_target_bound_metadata() -> None:
    plan = slow_query_plan()

    with pytest.raises(EvidenceBuildError):
        build_evidence(
            task_id=_TASK,
            step=plan.steps[0],
            plan=plan,
            target=_LIVE_TARGET,
            result=_live_result(),
            surface=SURFACE,
            params=PARAMS,
            captured_at=_AT,
        )


def test_verified_test_evidence_preserves_only_gateway_signed_metadata() -> None:
    plan = slow_query_plan()
    result = _live_result()

    envelope = build_evidence(
        task_id=_TASK,
        step=plan.steps[0],
        plan=plan,
        target=_LIVE_TARGET,
        result=result,
        surface=SURFACE,
        params=PARAMS,
        captured_at=_AT,
        live_policy=_LIVE_POLICY,
    )

    assert envelope.source == _LIVE_POLICY.evidence_source_ref
    assert envelope.limitations[-5:] == result.limitations
    assert envelope.redaction_ref is None
    assert "untrusted-adapter-source" not in envelope.model_dump_json()


@pytest.mark.parametrize(
    "target",
    [
        _LIVE_TARGET.model_copy(update={"environment_id": "dev"}),
        _LIVE_TARGET.model_copy(update={"resource_ids": ("other-test-cluster",)}),
        _LIVE_TARGET.model_copy(
            update={"resource_ids": ("approved-test-cluster", "another")}
        ),
    ],
)
def test_verified_test_evidence_rejects_wrong_or_non_unique_target(
    target: ResolvedTarget,
) -> None:
    plan = slow_query_plan()
    with pytest.raises(EvidenceBuildError):
        build_evidence(
            task_id=_TASK,
            step=plan.steps[0],
            plan=plan,
            target=target,
            result=_live_result(),
            surface=SURFACE,
            params=PARAMS,
            captured_at=_AT,
            live_policy=_LIVE_POLICY,
        )


def test_verified_test_evidence_rejects_metadata_or_source_drift() -> None:
    plan = slow_query_plan()
    result = _live_result()
    changed_pairs = (
        (_live_result(source_ref="other-source"), _LIVE_POLICY),
        (
            result,
            SlowQueryEvidencePolicy(
                approved_target=_LIVE_TARGET,
                target_fingerprint=_LIVE_POLICY.target_fingerprint,
                evidence_source_ref=_LIVE_POLICY.evidence_source_ref,
                config_revision="b" * 64,
                physical_identity_ref=_LIVE_POLICY.physical_identity_ref,
                driver_version=_LIVE_POLICY.driver_version,
                redaction_ref=None,
            ),
        ),
    )
    for changed, policy in changed_pairs:
        with pytest.raises(EvidenceBuildError):
            build_evidence(
                task_id=_TASK,
                step=plan.steps[0],
                plan=plan,
                target=_LIVE_TARGET,
                result=changed,
                surface=SURFACE,
                params=PARAMS,
                captured_at=_AT,
                live_policy=policy,
            )


def test_verified_test_failure_evidence_is_attributed_but_not_verified() -> None:
    plan = slow_query_plan()
    result = _live_result(rows=(), status=AdapterStatus.ERROR)

    envelope = build_evidence(
        task_id=_TASK,
        step=plan.steps[0],
        plan=plan,
        target=_LIVE_TARGET,
        result=result,
        surface=SURFACE,
        params=PARAMS,
        captured_at=_AT,
        live_policy=_LIVE_POLICY,
    )

    assert envelope.facts == ()
    assert envelope.limitations[-1] == "preflight=unverified"

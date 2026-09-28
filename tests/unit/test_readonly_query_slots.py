"""F1 SlotVerifier：目标唯一则用、0 个拒绝、多个追问，只认完全一致的回答（设计 §5.4）。"""

import datetime as dt
import hashlib
from typing import Any

import pytest

from xiaowei_agent.capabilities.readonly_query import (
    OP_EXECUTE_READONLY_QUERY,
    READONLY_QUERY_CAPABILITY_ID,
    READONLY_QUERY_CAPABILITY_VERSION,
    READONLY_QUERY_INPUT_SCHEMA_REF,
    ReadonlyQueryTargetCatalog,
    readonly_query_config_revision,
)
from xiaowei_agent.contracts import (
    Candidate,
    CapabilitySubject,
    ClarificationContext,
    ClarificationReasonCode,
    IntentDraft,
    IntentSource,
    InteractionRejectionReasonCode,
    RequestContext,
    ResourcesConfig,
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
    StarRocksResource,
)
from xiaowei_agent.contracts.clarification import TargetOption, TargetSelection
from xiaowei_agent.contracts.intent import READONLY_QUERY_INTENT
from xiaowei_agent.contracts.sql_query import SqlArtifactRef
from xiaowei_agent.planning.starrocks.readonly_query import (
    ReadonlyQueryParams,
    ReadonlyQuerySlotVerifier,
)

_SQL_HASH = hashlib.sha256(b"SELECT 1").hexdigest()
_ARTIFACT = SqlArtifactRef(sql_ref="ref-1", sql_hash=_SQL_HASH)
_CONTEXT = RequestContext(
    tenant_id="t1",
    actor="alice",
    environment_id="test",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-05.2",
)
_AS_OF = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
_CANDIDATE = Candidate(
    capability_id=READONLY_QUERY_CAPABILITY_ID,
    capability_version=READONLY_QUERY_CAPABILITY_VERSION,
    operation=OP_EXECUTE_READONLY_QUERY,
    score=1.0,
    match_evidence=("intent equals the declared capability id",),
    required_context=("tenant_id", "environment_id"),
)
_DRAFT = IntentDraft(
    intent=READONLY_QUERY_INTENT,
    slots={},
    missing=(),
    confidence=1.0,
    source=IntentSource.USER,
)


def _resource(resource_id: str, name: str, **overrides: Any) -> StarRocksResource:
    return StarRocksResource.model_validate(
        {
            "kind": "starrocks",
            "resource_id": resource_id,
            "environment": "test",
            "display_name": name,
            "host": "fe.example.com",
            "port": 9030,
            "database": "app",
            "username": "f1_reader",
            "tls_mode": "verify_identity",
            "enabled": True,
            "f1_enabled": True,
            **overrides,
        }
    )


_ORDERS = _resource("a" * 32, "Orders")
_METRICS = _resource("b" * 32, "指标库")


def _verifier(*resources: StarRocksResource) -> ReadonlyQuerySlotVerifier:
    config = ResourcesConfig(generation=1, resources=resources)
    return ReadonlyQuerySlotVerifier(
        catalog=ReadonlyQueryTargetCatalog.from_resources(tenant_id="t1", config=config)
    )


def _options() -> tuple[TargetOption, ...]:
    return (
        TargetOption(resource_id="a" * 32, display_name="Orders"),
        TargetOption(resource_id="b" * 32, display_name="指标库"),
    )


def _clarification(options: tuple[TargetOption, ...] | None = None) -> ClarificationContext:
    subject = CapabilitySubject(
        kind="capability",
        capability_id=READONLY_QUERY_CAPABILITY_ID,
        capability_version=READONLY_QUERY_CAPABILITY_VERSION,
        operation=OP_EXECUTE_READONLY_QUERY,
        input_schema_ref=READONLY_QUERY_INPUT_SCHEMA_REF,
        target_selection=TargetSelection(
            sql_ref=_ARTIFACT.sql_ref,
            sql_hash=_ARTIFACT.sql_hash,
            options=_options() if options is None else options,
        ),
    )
    return ClarificationContext(subject=subject, confirmed_slots=())


def _verify(
    verifier: ReadonlyQuerySlotVerifier,
    *,
    user_text: str = "",
    clarification: ClarificationContext | None = None,
    sql_artifact: SqlArtifactRef | None = _ARTIFACT,
    draft: IntentDraft = _DRAFT,
) -> object:
    return verifier(
        candidate=_CANDIDATE,
        draft=draft,
        context=_CONTEXT,
        as_of=_AS_OF,
        user_text=user_text,
        clarification=clarification,
        sql_artifact=sql_artifact,
    )


def test_a_single_target_is_used_directly() -> None:
    result = _verify(_verifier(_ORDERS))
    assert isinstance(result, SlotReady)
    assert result.params == ReadonlyQueryParams(
        sql_ref="ref-1",
        sql_hash=_SQL_HASH,
        resource_id="a" * 32,
        config_revision=readonly_query_config_revision(_ORDERS),
        budget=_ORDERS.f1_budget,
    )
    assert result.confirmed_slots == ()


def test_no_target_is_invalid() -> None:
    result = _verify(_verifier(_resource("a" * 32, "Orders", f1_enabled=False)))
    assert result == SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_TARGET_UNAVAILABLE
    )


def test_several_targets_ask_with_recorded_options() -> None:
    result = _verify(_verifier(_METRICS, _ORDERS))
    assert result == SlotIncomplete(
        reason_code=ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED,
        missing_fields=(),
        confirmed_slots=(),
        target_selection=TargetSelection(
            sql_ref="ref-1", sql_hash=_SQL_HASH, options=_options()
        ),
    )


@pytest.mark.parametrize(("answer", "resource_id"), [("Orders", "a" * 32), ("指标库", "b" * 32)])
def test_an_exact_answer_selects_that_target(answer: str, resource_id: str) -> None:
    result = _verify(
        _verifier(_ORDERS, _METRICS),
        user_text=answer,
        clarification=_clarification(),
        sql_artifact=None,
    )
    assert isinstance(result, SlotReady)
    assert result.params.resource_id == resource_id
    assert result.params.sql_ref == "ref-1"


@pytest.mark.parametrize(
    "answer",
    ["orders", "ORDERS", "Orders ", " Orders", "Order", "Orders库", "1", "第一个", "指标", ""],
)
def test_any_other_answer_asks_again_without_guessing(answer: str) -> None:
    result = _verify(
        _verifier(_ORDERS, _METRICS),
        user_text=answer,
        clarification=_clarification(),
        sql_artifact=None,
    )
    assert isinstance(result, SlotIncomplete)
    assert result.reason_code is ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED
    assert result.target_selection == _clarification().subject.target_selection


def test_changed_target_set_since_the_question_is_invalid() -> None:
    renamed = _resource("b" * 32, "指标库v2")
    result = _verify(
        _verifier(_ORDERS, renamed),
        user_text="Orders",
        clarification=_clarification(),
        sql_artifact=None,
    )
    assert result == SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_TARGET_UNAVAILABLE
    )


def test_without_a_trusted_sql_reference_nothing_is_ready() -> None:
    # 模型或关键词规则给出的同名意图没有 SqlArtifact：不能凭空得到可执行参数。
    result = _verify(_verifier(_ORDERS), sql_artifact=None)
    assert result == SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID
    )


def test_a_clarification_without_target_selection_is_invalid() -> None:
    subject = CapabilitySubject(
        kind="capability",
        capability_id=READONLY_QUERY_CAPABILITY_ID,
        capability_version=READONLY_QUERY_CAPABILITY_VERSION,
        operation=OP_EXECUTE_READONLY_QUERY,
        input_schema_ref=READONLY_QUERY_INPUT_SCHEMA_REF,
    )
    result = _verify(
        _verifier(_ORDERS, _METRICS),
        user_text="Orders",
        clarification=ClarificationContext(subject=subject, confirmed_slots=()),
        sql_artifact=None,
    )
    assert isinstance(result, SlotInvalid)


def test_two_sql_sources_are_invalid() -> None:
    result = _verify(
        _verifier(_ORDERS, _METRICS),
        user_text="Orders",
        clarification=_clarification(),
        sql_artifact=_ARTIFACT,
    )
    assert isinstance(result, SlotInvalid)


def test_a_draft_with_slots_is_invalid() -> None:
    draft = IntentDraft(
        intent=READONLY_QUERY_INTENT,
        slots={"database": "app"},
        missing=(),
        confidence=1.0,
        source=IntentSource.USER,
    )
    assert isinstance(_verify(_verifier(_ORDERS), draft=draft), SlotInvalid)


def test_other_tenant_or_environment_sees_no_target() -> None:
    verifier = _verifier(_ORDERS)
    for context in (
        _CONTEXT.model_copy(update={"tenant_id": "t2"}),
        _CONTEXT.model_copy(update={"environment_id": "dev"}),
    ):
        result = verifier(
            candidate=_CANDIDATE,
            draft=_DRAFT,
            context=context,
            as_of=_AS_OF,
            user_text="",
            sql_artifact=_ARTIFACT,
        )
        assert isinstance(result, SlotInvalid)


def _other_capability_cases() -> list[tuple[str, object, str]]:
    from xiaowei_agent.contracts.intent import (
        ASSET_INVENTORY_INTENT,
        PROMETHEUS_ALERT_INTENT,
        SLOW_QUERY_INTENT,
    )
    from xiaowei_agent.planning.assets.slots import verify_asset_lookup_slots
    from xiaowei_agent.planning.prometheus.slots import verify_prometheus_alert_slots
    from xiaowei_agent.planning.starrocks.slots import verify_slow_query_slots

    return [
        (SLOW_QUERY_INTENT, verify_slow_query_slots, "最近30分钟有哪些慢查询"),
        (
            PROMETHEUS_ALERT_INTENT,
            verify_prometheus_alert_slots,
            "查告警 HostHighCpu 在 node-1.example.com:9100 的证据",
        ),
        (ASSET_INVENTORY_INTENT, verify_asset_lookup_slots, "查资产 hostname=node-1.example.com"),
    ]


@pytest.mark.parametrize(("intent", "verifier", "text"), _other_capability_cases())
def test_other_capabilities_refuse_an_sql_reference(
    intent: str, verifier: Any, text: str
) -> None:
    from xiaowei_agent.capabilities.intent import RuleBasedIntentInterpreter

    draft = RuleBasedIntentInterpreter().interpret(text=text, context=_CONTEXT)
    assert draft.intent == intent
    kwargs = {
        "candidate": _CANDIDATE,
        "draft": draft,
        "context": _CONTEXT,
        "as_of": _AS_OF,
        "user_text": text,
    }
    assert isinstance(verifier(**kwargs), SlotReady)
    assert isinstance(verifier(**kwargs, sql_artifact=_ARTIFACT), SlotInvalid)

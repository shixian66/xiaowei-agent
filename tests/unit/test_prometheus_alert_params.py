"""Prometheus 告警参数只接受固定模板所需的闭集输入。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.application.capability_input import (
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
)
from xiaowei_agent.application.default_capabilities import PROMETHEUS_ALERT_BINDING
from xiaowei_agent.capabilities.prometheus_alert import (
    OP_GET_ACTIVE_ALERTS,
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMETHEUS_ALERT_CAPABILITY_VERSION,
    PROMETHEUS_ALERT_INPUT_SCHEMA_REF,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    CapabilitySubject,
    ClarificationContext,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedSlot,
    IntentDraft,
    IntentSource,
    RequestContext,
)
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams
from xiaowei_agent.planning.slot_verification import (
    canonical_time_range_value,
    confirmed_text_value,
    require_confirmed_projection,
)

_END = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
_AS_OF = dt.datetime(2026, 9, 5, 12, 0, 37, tzinfo=dt.UTC)
_SNAPSHOT = StaticCapabilityRegistry().snapshot()
_CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)


def _params(**overrides: object) -> PrometheusAlertParams:
    values: dict[str, object] = {
        "alert_name": "HostHighCpu",
        "instance": "NODE-1.EXAMPLE.COM.:9100",
        "window_start": _END - dt.timedelta(minutes=30),
        "window_end": _END,
    }
    return PrometheusAlertParams(**(values | overrides))


def _draft(
    *,
    source: IntentSource = IntentSource.USER,
    missing: tuple[str, ...] = (),
    **slots: str,
) -> IntentDraft:
    return IntentDraft(
        intent=PROMETHEUS_ALERT_CAPABILITY_ID,
        slots=slots,
        missing=missing,
        confidence=0.9,
        source=source,
    )


def _candidate() -> object:
    return next(
        item
        for item in DeterministicCapabilityResolver()
        .resolve(
            draft=_draft(alert_name="HostHighCpu", instance="node-1.example.com:9100"),
            context=_CONTEXT,
            snapshot=_SNAPSHOT,
        )
        .items
        if item.operation == OP_GET_ACTIVE_ALERTS
    )


def _verify(
    draft: IntentDraft,
    *,
    clarification: ClarificationContext | None = None,
    user_text: str | None = None,
) -> SlotReady[PrometheusAlertParams] | SlotIncomplete | SlotInvalid:
    result = PROMETHEUS_ALERT_BINDING.input_binding.slot_verifier(
        candidate=_candidate(),
        draft=draft,
        context=_CONTEXT,
        as_of=_AS_OF,
        user_text=user_text or _user_text_from_draft(draft),
        clarification=clarification,
    )
    assert isinstance(result, SlotReady | SlotIncomplete | SlotInvalid)
    return result


def _ready(
    draft: IntentDraft,
    *,
    clarification: ClarificationContext | None = None,
    user_text: str | None = None,
) -> SlotReady[PrometheusAlertParams]:
    result = _verify(draft, clarification=clarification, user_text=user_text)
    assert isinstance(result, SlotReady)
    return result


def _user_text_from_draft(draft: IntentDraft) -> str:
    if draft.source is not IntentSource.USER:
        return "查告警证据"
    alert_name = draft.slots.get("alert_name")
    instance = draft.slots.get("instance")
    parts = ["查告警"]
    if alert_name:
        parts.append(alert_name)
    if instance:
        parts.append(f"在 {instance}")
    parts.append("的证据")
    if fingerprint := draft.slots.get("fingerprint"):
        parts.append(f"fingerprint={fingerprint}")
    if window := draft.slots.get("window_minutes"):
        parts.append(f"最近{window}分钟")
    return " ".join(parts)


def _slot(field: ClarificationField, value: object) -> ConfirmedSlot:
    return ConfirmedSlot(field=field, value=value)


def _snapshot(*slots: ConfirmedSlot) -> tuple[ConfirmedSlot, ...]:
    return tuple(sorted(slots, key=lambda slot: slot.field.value))


def _clarification(*slots: ConfirmedSlot) -> ClarificationContext:
    confirmed = _snapshot(*slots)
    subject = CapabilitySubject(
        kind="capability",
        capability_id=PROMETHEUS_ALERT_CAPABILITY_ID,
        capability_version=PROMETHEUS_ALERT_CAPABILITY_VERSION,
        operation=OP_GET_ACTIVE_ALERTS,
        input_schema_ref=PROMETHEUS_ALERT_INPUT_SCHEMA_REF,
        confirmed_slots=confirmed,
    )
    return ClarificationContext(subject=subject, confirmed_slots=confirmed)


def test_prometheus_verifier_returns_incomplete_for_missing_required_fields() -> None:
    result = _verify(_draft(alert_name="HostHighCpu", missing=("instance",)))

    assert isinstance(result, SlotIncomplete)
    assert result.reason_code is ClarificationReasonCode.CAPABILITY_FIELDS_MISSING
    assert result.missing_fields == (ClarificationField.INSTANCE,)
    assert tuple(slot.field for slot in result.confirmed_slots) == (
        ClarificationField.ALERT_NAME,
    )


def test_prometheus_verifier_does_not_trust_model_supplied_required_slots() -> None:
    result = _verify(
        _draft(
            source=IntentSource.MODEL,
            alert_name="HostHighCpu",
            instance="node-1.example.com:9100",
        )
    )

    assert isinstance(result, SlotIncomplete)
    assert result.missing_fields == (
        ClarificationField.ALERT_NAME,
        ClarificationField.INSTANCE,
    )


def test_prometheus_verifier_promotes_and_canonicalizes_user_slots() -> None:
    ready = _ready(
        _draft(
            alert_name="HostHighCpu",
            instance="NODE-1.EXAMPLE.COM.:9100",
            fingerprint="fp-001",
            window_minutes="45",
        )
    )

    assert ready.params.alert_name == "HostHighCpu"
    assert ready.params.instance == "node-1.example.com:9100"
    assert ready.params.fingerprint == "fp-001"
    assert ready.params.window_start == dt.datetime(2026, 9, 5, 11, 15, tzinfo=dt.UTC)
    assert ready.params.window_end == _END
    assert tuple(slot.field for slot in ready.confirmed_slots) == (
        ClarificationField.ALERT_NAME,
        ClarificationField.FINGERPRINT,
        ClarificationField.INSTANCE,
        ClarificationField.TIME_RANGE,
    )


def test_prometheus_verifier_inherits_parent_and_current_user_completes_it() -> None:
    parent = _clarification(
        _slot(ClarificationField.ALERT_NAME, confirmed_text_value("HostHighCpu")),
        _slot(
            ClarificationField.TIME_RANGE,
            canonical_time_range_value(
                start_utc=dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.UTC),
                end_utc=dt.datetime(2026, 9, 5, 10, 0, tzinfo=dt.UTC),
            ),
        ),
    )

    ready = _ready(
        _draft(instance="NODE-1.EXAMPLE.COM.:9100"), clarification=parent
    )

    assert ready.params.alert_name == "HostHighCpu"
    assert ready.params.instance == "node-1.example.com:9100"
    assert ready.params.window_start == dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.UTC)
    assert ready.params.window_end == dt.datetime(2026, 9, 5, 10, 0, tzinfo=dt.UTC)


def test_prometheus_verifier_rejects_invalid_current_slots() -> None:
    result = _verify(
        _draft(alert_name="HostHighCpu"),
        user_text='查告警 HostHighCpu 的证据 instance=node"}',
    )

    assert isinstance(result, SlotInvalid)


def test_prometheus_projector_covers_confirmed_slots_from_plan_and_target() -> None:
    ready = _ready(
        _draft(
            alert_name="HostHighCpu",
            instance="NODE-1.EXAMPLE.COM.:9100",
            fingerprint="fp-001",
        )
    )
    prepared = PROMETHEUS_ALERT_BINDING.input_binding.planner(
        candidate=_candidate(),
        params=ready.params,
        context=_CONTEXT,
        snapshot=_SNAPSHOT,
    )
    projector = PROMETHEUS_ALERT_BINDING.input_binding.confirmed_slot_projector
    assert projector is not None

    projected = projector(plan=prepared.plan, target=prepared.target)

    assert require_confirmed_projection(
        confirmed=ready.confirmed_slots, projected=projected
    ) == projected


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("NODE-1.EXAMPLE.COM.:9100", "node-1.example.com:9100"),
        ("10.0.0.008:9100", None),
        ("10.0.0.8:9100", "10.0.0.8:9100"),
        ("[2001:0DB8::1]:9100", "[2001:db8::1]:9100"),
        ("[2001:db8::1]", "[2001:db8::1]"),
    ],
)
def test_instance_canonicalization(raw: str, canonical: str | None) -> None:
    if canonical is None:
        with pytest.raises(ValidationError):
            _params(instance=raw)
    else:
        assert _params(instance=raw).instance == canonical


@pytest.mark.parametrize(
    "instance",
    [
        "node-*",
        "node{job=x}",
        'node"} or up{instance="x',
        "node\\name",
        "node\nname",
        "2001:db8::1",
        "node:0",
        "node:65536",
        "-node:9100",
    ],
)
def test_hostile_or_ambiguous_instances_are_rejected(instance: str) -> None:
    with pytest.raises(ValidationError):
        _params(instance=instance)


@pytest.mark.parametrize(
    "overrides",
    [
        {"alert_name": "UnknownAlert"},
        {"fingerprint": "bad value"},
        {"window_start": _END},
        {"window_start": _END - dt.timedelta(minutes=361)},
        {"window_start": dt.datetime(2026, 9, 5, 11, 30)},
        {"step_seconds": 30},
        {"max_series": 6},
        {"max_points_per_series": 362},
    ],
)
def test_closed_values_and_budgets_are_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _params(**overrides)


def test_typed_arguments_round_trip_preserves_the_canonical_form() -> None:
    original = _params(fingerprint="fp-001")
    restored = PrometheusAlertParams.from_typed_arguments(
        original.to_typed_arguments()
        | {
            "promql": 'up{instance="node-1.example.com:9100"}',
            "promql_template_id": "prometheus.alert.instance_up.v1",
        }
    )
    assert restored == original


@pytest.mark.parametrize("key", ["gateway", "operation", "template_id", "raw_promql"])
def test_unknown_typed_arguments_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        PrometheusAlertParams.from_typed_arguments(
            _params().to_typed_arguments() | {key: "polluted"}
        )

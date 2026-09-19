"""Prometheus alert trusted slot upgrade and disclosure projection."""

import datetime as dt
from collections.abc import Mapping
from typing import Final

from pydantic import TypeAdapter, ValidationError

from xiaowei_agent.capabilities.intent import extract_slots_for_intent
from xiaowei_agent.capabilities.prometheus_alert import (
    OP_GET_ACTIVE_ALERTS,
    OP_QUERY_METRIC_RANGE,
)
from xiaowei_agent.contracts import (
    Candidate,
    ClarificationContext,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedSlot,
    ConfirmedTextValue,
    ConfirmedTimeRangeValue,
    ExecutionPlan,
    IntentDraft,
    InteractionRejectionReasonCode,
    RequestContext,
    ResolvedTarget,
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
)
from xiaowei_agent.planning.prometheus.params import (
    DEFAULT_WINDOW_MINUTES,
    AlertName,
    Fingerprint,
    Instance,
    PrometheusAlertParams,
    normalise_window,
)
from xiaowei_agent.planning.prometheus.target import (
    PROVIDER,
    RESOURCE_KIND,
    SELECTOR_VERSION,
)
from xiaowei_agent.planning.slot_verification import (
    SlotCandidate,
    SlotSource,
    SlotVerificationError,
    canonical_time_range_value,
    confirmed_text_value,
    merge_confirmed_slots,
    validate_confirmed_snapshot,
)

PROMETHEUS_ALERT_CLARIFICATION_FIELDS: Final[frozenset[ClarificationField]] = (
    frozenset(
        {
            ClarificationField.TIME_RANGE,
            ClarificationField.ALERT_NAME,
            ClarificationField.INSTANCE,
            ClarificationField.FINGERPRINT,
        }
    )
)

_REQUIRED_FIELDS: Final[tuple[ClarificationField, ...]] = (
    ClarificationField.ALERT_NAME,
    ClarificationField.INSTANCE,
)

_SLOT_FIELD_BY_NAME: Final[Mapping[str, ClarificationField]] = {
    "alert_name": ClarificationField.ALERT_NAME,
    "instance": ClarificationField.INSTANCE,
    "fingerprint": ClarificationField.FINGERPRINT,
}

_PARAM_FIELD_BY_SLOT: Final[Mapping[ClarificationField, str]] = {
    ClarificationField.ALERT_NAME: "alert_name",
    ClarificationField.INSTANCE: "instance",
    ClarificationField.FINGERPRINT: "fingerprint",
}

_ALERT_NAME_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(AlertName)
_INSTANCE_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(Instance)
_FINGERPRINT_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(Fingerprint)


def verify_prometheus_alert_slots(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    user_text: str,
    clarification: ClarificationContext | None = None,
) -> SlotReady[PrometheusAlertParams] | SlotIncomplete | SlotInvalid:
    """Upgrade only trusted alert slots into typed Prometheus params."""
    _ = (candidate, context)
    try:
        parent = () if clarification is None else clarification.confirmed_slots
        slots = dict(extract_slots_for_intent(text=user_text, intent=draft.intent))
        for slot_name, value in _single_missing_text_slot(
            user_text, clarification=clarification
        ).items():
            slots.setdefault(slot_name, value)
        confirmed = merge_confirmed_slots(
            parent_snapshot=parent,
            current_candidates=_current_user_candidates(slots=slots, as_of=as_of),
            allowed_fields=PROMETHEUS_ALERT_CLARIFICATION_FIELDS,
        )
        missing = tuple(
            field
            for field in _REQUIRED_FIELDS
            if all(slot.field is not field for slot in confirmed)
        )
        if missing:
            return SlotIncomplete(
                reason_code=ClarificationReasonCode.CAPABILITY_FIELDS_MISSING,
                missing_fields=missing,
                confirmed_slots=confirmed,
            )
        return SlotReady(
            params=_params_from_confirmed_slots(confirmed=confirmed, as_of=as_of),
            confirmed_slots=confirmed,
        )
    except (SlotVerificationError, ValueError, ValidationError):
        return _invalid()


def project_prometheus_alert_confirmed_slots(
    *,
    plan: ExecutionPlan,
    target: ResolvedTarget,
) -> tuple[ConfirmedSlot, ...]:
    """Reconstruct canonical disclosure slots from plan params and target."""
    params = _params_from_plan(plan)
    _validate_prometheus_target(target=target, params=params)
    slots = [
        ConfirmedSlot(
            field=ClarificationField.ALERT_NAME,
            value=confirmed_text_value(params.alert_name),
        ),
        ConfirmedSlot(
            field=ClarificationField.INSTANCE,
            value=confirmed_text_value(params.instance),
        ),
        ConfirmedSlot(
            field=ClarificationField.TIME_RANGE,
            value=canonical_time_range_value(
                start_utc=params.window_start,
                end_utc=params.window_end,
            ),
        ),
    ]
    if params.fingerprint is not None:
        slots.append(
            ConfirmedSlot(
                field=ClarificationField.FINGERPRINT,
                value=confirmed_text_value(params.fingerprint),
            )
        )
    return validate_confirmed_snapshot(
        tuple(sorted(slots, key=lambda slot: slot.field.value)),
        allowed_fields=PROMETHEUS_ALERT_CLARIFICATION_FIELDS,
    )


def _current_user_candidates(
    *,
    slots: Mapping[str, str],
    as_of: dt.datetime,
) -> tuple[SlotCandidate, ...]:
    candidates: list[SlotCandidate] = []
    raw_window = slots.get("window_minutes")
    if raw_window is not None:
        if not raw_window.isdigit():
            raise SlotVerificationError("window must be numeric minutes")
        start, end = normalise_window(as_of=as_of, window_minutes=int(raw_window))
        candidates.append(
            SlotCandidate(
                field=ClarificationField.TIME_RANGE,
                value=canonical_time_range_value(start_utc=start, end_utc=end),
                source=SlotSource.CURRENT_TEXT,
            )
        )
    for slot_name, field in _SLOT_FIELD_BY_NAME.items():
        raw = slots.get(slot_name)
        if raw is not None:
            candidates.append(
                SlotCandidate(
                    field=field,
                    value=confirmed_text_value(_canonical_text(field=field, raw=raw)),
                    source=SlotSource.CURRENT_TEXT,
                )
            )
    return tuple(candidates)


def _single_missing_text_slot(
    user_text: str, *, clarification: ClarificationContext | None
) -> dict[str, str]:
    if clarification is None or len(clarification.missing_fields) != 1:
        return {}
    slot_name = _PARAM_FIELD_BY_SLOT.get(clarification.missing_fields[0])
    value = user_text.strip()
    if slot_name is None or not value:
        return {}
    return {slot_name: value}


def _params_from_confirmed_slots(
    *,
    confirmed: tuple[ConfirmedSlot, ...],
    as_of: dt.datetime,
) -> PrometheusAlertParams:
    start, end = normalise_window(
        as_of=as_of,
        window_minutes=DEFAULT_WINDOW_MINUTES,
    )
    payload: dict[str, object] = {
        "window_start": start,
        "window_end": end,
    }
    for slot in confirmed:
        if slot.field is ClarificationField.TIME_RANGE:
            value = slot.value
            if not isinstance(value, ConfirmedTimeRangeValue):
                raise SlotVerificationError("time_range slot has wrong value type")
            payload["window_start"] = value.start_utc
            payload["window_end"] = value.end_utc
            continue
        attr = _PARAM_FIELD_BY_SLOT.get(slot.field)
        if attr is None:
            raise SlotVerificationError("confirmed slot field is not allowed")
        value = slot.value
        if not isinstance(value, ConfirmedTextValue):
            raise SlotVerificationError("text slot has wrong value type")
        payload[attr] = _canonical_text(field=slot.field, raw=value.text)
    return PrometheusAlertParams.model_validate(payload)


def _params_from_plan(plan: ExecutionPlan) -> PrometheusAlertParams:
    metric = next(
        (item for item in plan.steps if item.operation == OP_QUERY_METRIC_RANGE),
        None,
    )
    alert = next(
        (item for item in plan.steps if item.operation == OP_GET_ACTIVE_ALERTS),
        None,
    )
    if metric is None or alert is None:
        raise SlotVerificationError("prometheus alert plan steps are incomplete")
    params = PrometheusAlertParams.from_typed_arguments(metric.typed_arguments)
    for key in ("alert_name", "instance"):
        if alert.typed_arguments.get(key) != getattr(params, key):
            raise SlotVerificationError("alert and metric arguments differ")
    fingerprint = alert.typed_arguments.get("fingerprint")
    if fingerprint is None:
        return params
    payload = params.model_dump(mode="python") | {"fingerprint": fingerprint}
    return PrometheusAlertParams.model_validate(payload)


def _canonical_text(*, field: ClarificationField, raw: str) -> str:
    if field is ClarificationField.ALERT_NAME:
        return _ALERT_NAME_ADAPTER.validate_python(raw)
    if field is ClarificationField.INSTANCE:
        return _INSTANCE_ADAPTER.validate_python(raw)
    if field is ClarificationField.FINGERPRINT:
        return _FINGERPRINT_ADAPTER.validate_python(raw)
    raise SlotVerificationError("confirmed slot field is not allowed")


def _validate_prometheus_target(
    *,
    target: ResolvedTarget,
    params: PrometheusAlertParams,
) -> None:
    if (
        target.provider != PROVIDER
        or target.resource_kind != RESOURCE_KIND
        or target.selector_version != SELECTOR_VERSION
        or target.resource_ids
        != (f"alert:{params.alert_name}", f"instance:{params.instance}")
    ):
        raise SlotVerificationError("prometheus alert target is incompatible")


def _invalid() -> SlotInvalid:
    return SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID
    )

"""StarRocks slow-query trusted slot upgrade and disclosure projection."""

import datetime as dt
from collections.abc import Mapping
from typing import Final

from pydantic import ValidationError

from xiaowei_agent.capabilities.intent import extract_slots_for_intent
from xiaowei_agent.capabilities.specs import OP_LIST
from xiaowei_agent.capabilities.target import (
    PROVIDER,
    RESOURCE_KIND,
    SELECTOR_VERSION,
)
from xiaowei_agent.contracts import (
    Candidate,
    ClarificationContext,
    ClarificationField,
    ConfirmedSlot,
    ConfirmedTextValue,
    ConfirmedTimeRangeValue,
    ExecutionPlan,
    IntentDraft,
    InteractionRejectionReasonCode,
    RequestContext,
    ResolvedTarget,
    SlotInvalid,
    SlotReady,
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
from xiaowei_agent.planning.starrocks.params import (
    DEFAULT_MIN_QUERY_TIME_MS,
    DEFAULT_ROW_LIMIT,
    DEFAULT_WINDOW_MINUTES,
    SlowQueryParams,
    normalise_window,
)

SLOW_QUERY_CLARIFICATION_FIELDS: Final[frozenset[ClarificationField]] = frozenset(
    {
        ClarificationField.TIME_RANGE,
        ClarificationField.DATABASE,
        ClarificationField.USER_NAME,
        ClarificationField.QUERY_ID,
    }
)

_SLOT_FIELD_BY_NAME: Final[Mapping[str, ClarificationField]] = {
    "database": ClarificationField.DATABASE,
    "user_name": ClarificationField.USER_NAME,
    "query_id": ClarificationField.QUERY_ID,
}

_PARAM_FIELD_BY_SLOT: Final[Mapping[ClarificationField, str]] = {
    ClarificationField.DATABASE: "database",
    ClarificationField.USER_NAME: "user_name",
    ClarificationField.QUERY_ID: "query_id",
}


def verify_slow_query_slots(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    user_text: str,
    clarification: ClarificationContext | None = None,
) -> SlotReady[SlowQueryParams] | SlotInvalid:
    """Upgrade only trusted slow-query slots into typed params."""
    _ = (candidate, context)
    try:
        parent = () if clarification is None else clarification.confirmed_slots
        slots = extract_slots_for_intent(text=user_text, intent=draft.intent)
        confirmed = merge_confirmed_slots(
            parent_snapshot=parent,
            current_candidates=_current_user_candidates(slots=slots, as_of=as_of),
            allowed_fields=SLOW_QUERY_CLARIFICATION_FIELDS,
        )
        return SlotReady(
            params=_params_from_confirmed_slots(confirmed=confirmed, as_of=as_of),
            confirmed_slots=confirmed,
        )
    except (SlotVerificationError, ValueError, ValidationError):
        return _invalid()


def project_slow_query_confirmed_slots(
    *,
    plan: ExecutionPlan,
    target: ResolvedTarget,
) -> tuple[ConfirmedSlot, ...]:
    """Reconstruct canonical disclosure slots from plan params and target."""
    _validate_slow_query_target(target)
    params = _params_from_plan(plan)
    slots = [
        ConfirmedSlot(
            field=ClarificationField.TIME_RANGE,
            value=canonical_time_range_value(
                start_utc=params.window_start,
                end_utc=params.window_end,
            ),
        )
    ]
    for field, attr in _PARAM_FIELD_BY_SLOT.items():
        value = getattr(params, attr)
        if value is not None:
            slots.append(ConfirmedSlot(field=field, value=confirmed_text_value(value)))
    return validate_confirmed_snapshot(
        tuple(sorted(slots, key=lambda slot: slot.field.value)),
        allowed_fields=SLOW_QUERY_CLARIFICATION_FIELDS,
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
        value = slots.get(slot_name)
        if value is not None:
            candidates.append(
                SlotCandidate(
                    field=field,
                    value=confirmed_text_value(value),
                    source=SlotSource.CURRENT_TEXT,
                )
            )
    return tuple(candidates)


def _params_from_confirmed_slots(
    *,
    confirmed: tuple[ConfirmedSlot, ...],
    as_of: dt.datetime,
) -> SlowQueryParams:
    start, end = normalise_window(
        as_of=as_of,
        window_minutes=DEFAULT_WINDOW_MINUTES,
    )
    payload: dict[str, object] = {
        "window_start": start,
        "window_end": end,
        "min_query_time_ms": DEFAULT_MIN_QUERY_TIME_MS,
        "row_limit": DEFAULT_ROW_LIMIT,
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
        payload[attr] = value.text
    return SlowQueryParams.model_validate(payload)


def _params_from_plan(plan: ExecutionPlan) -> SlowQueryParams:
    step = next((item for item in plan.steps if item.operation == OP_LIST), None)
    if step is None:
        raise SlotVerificationError("slow-query list step is missing")
    return SlowQueryParams.from_typed_arguments(step.typed_arguments)


def _validate_slow_query_target(target: ResolvedTarget) -> None:
    if (
        target.provider != PROVIDER
        or target.resource_kind != RESOURCE_KIND
        or target.selector_version != SELECTOR_VERSION
    ):
        raise SlotVerificationError("slow-query target is incompatible")


def _invalid() -> SlotInvalid:
    return SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID
    )

"""Asset lookup trusted selector upgrade and disclosure projection."""

import datetime as dt
from collections.abc import Mapping
from typing import Final

from pydantic import TypeAdapter, ValidationError

from xiaowei_agent.capabilities.intent import extract_slots_for_intent
from xiaowei_agent.contracts import (
    Candidate,
    ClarificationContext,
    ClarificationField,
    ClarificationReasonCode,
    ConfirmedSlot,
    ConfirmedTextValue,
    ExecutionPlan,
    IntentDraft,
    InteractionRejectionReasonCode,
    RequestContext,
    ResolvedTarget,
    SlotIncomplete,
    SlotInvalid,
    SlotReady,
)
from xiaowei_agent.planning.assets.compiler import (
    ASSET_FIELD_SET_ID,
    ASSET_LOOKUP_LIMIT,
    ASSET_PROVIDER,
    ASSET_RESOURCE_KIND,
    ASSET_SELECTOR_VERSION,
    ASSET_STEP_ID,
)
from xiaowei_agent.planning.assets.params import (
    AssetId,
    AssetLookupParams,
    Hostname,
    IpAddress,
)
from xiaowei_agent.planning.slot_verification import (
    SlotCandidate,
    SlotSource,
    SlotVerificationError,
    confirmed_text_value,
    merge_confirmed_slots,
    validate_confirmed_snapshot,
)

ASSET_CLARIFICATION_FIELDS: Final[frozenset[ClarificationField]] = frozenset(
    {
        ClarificationField.ASSET_ID,
        ClarificationField.HOSTNAME,
        ClarificationField.IP,
    }
)
ASSET_SELECTOR_ALTERNATIVES: Final[tuple[ClarificationField, ...]] = (
    ClarificationField.ASSET_ID,
    ClarificationField.HOSTNAME,
    ClarificationField.IP,
)

_SLOT_FIELD_BY_NAME: Final[Mapping[str, ClarificationField]] = {
    "asset_id": ClarificationField.ASSET_ID,
    "hostname": ClarificationField.HOSTNAME,
    "ip": ClarificationField.IP,
}

_PARAM_FIELD_BY_SLOT: Final[Mapping[ClarificationField, str]] = {
    ClarificationField.ASSET_ID: "asset_id",
    ClarificationField.HOSTNAME: "hostname",
    ClarificationField.IP: "ip",
}

_FIELD_BY_SELECTOR_KIND: Final[Mapping[str, ClarificationField]] = {
    value: key for key, value in _PARAM_FIELD_BY_SLOT.items()
}

_ASSET_ID_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(AssetId)
_HOSTNAME_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(Hostname)
_IP_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(IpAddress)


def verify_asset_lookup_slots(
    *,
    candidate: Candidate,
    draft: IntentDraft,
    context: RequestContext,
    as_of: dt.datetime,
    user_text: str,
    clarification: ClarificationContext | None = None,
) -> SlotReady[AssetLookupParams] | SlotIncomplete | SlotInvalid:
    """Upgrade exactly one trusted asset selector into typed params."""
    _ = (candidate, context, as_of)
    try:
        parent = () if clarification is None else clarification.confirmed_slots
        slots = dict(extract_slots_for_intent(text=user_text, intent=draft.intent))
        for slot_name, value in _single_missing_selector_slot(
            user_text,
            slots=slots,
            clarification=clarification,
        ).items():
            slots.setdefault(slot_name, value)
        confirmed = merge_confirmed_slots(
            parent_snapshot=parent,
            current_candidates=_current_user_candidates(slots),
            allowed_fields=ASSET_CLARIFICATION_FIELDS,
        )
        if not confirmed:
            return SlotIncomplete(
                reason_code=ClarificationReasonCode.CAPABILITY_ASSET_SELECTOR_REQUIRED,
                missing_fields=ASSET_SELECTOR_ALTERNATIVES,
                confirmed_slots=(),
            )
        return SlotReady(
            params=_params_from_confirmed_slots(confirmed),
            confirmed_slots=confirmed,
        )
    except (SlotVerificationError, ValueError, ValidationError):
        return _invalid()


def project_asset_lookup_confirmed_slots(
    *,
    plan: ExecutionPlan,
    target: ResolvedTarget,
) -> tuple[ConfirmedSlot, ...]:
    """Reconstruct canonical selector disclosure from plan and target."""
    step = next((item for item in plan.steps if item.step_id == ASSET_STEP_ID), None)
    if step is None:
        raise SlotVerificationError("asset lookup step is missing")
    if (
        step.typed_arguments.get("field_set_id") != ASSET_FIELD_SET_ID
        or step.typed_arguments.get("limit") != ASSET_LOOKUP_LIMIT
    ):
        raise SlotVerificationError("asset lookup step arguments are incompatible")
    selector_kind = step.typed_arguments.get("selector_kind")
    selector_value = step.typed_arguments.get("selector_value")
    if not isinstance(selector_kind, str) or not isinstance(selector_value, str):
        raise SlotVerificationError("asset selector arguments are malformed")
    field = _FIELD_BY_SELECTOR_KIND.get(selector_kind)
    if field is None:
        raise SlotVerificationError("asset selector kind is not allowed")
    canonical = _canonical_text(field=field, raw=selector_value)
    _validate_asset_target(target=target, selector_kind=selector_kind, value=canonical)
    return validate_confirmed_snapshot(
        (ConfirmedSlot(field=field, value=confirmed_text_value(canonical)),),
        allowed_fields=ASSET_CLARIFICATION_FIELDS,
    )


def _current_user_candidates(slots: Mapping[str, str]) -> tuple[SlotCandidate, ...]:
    candidates: list[SlotCandidate] = []
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


def _single_missing_selector_slot(
    user_text: str,
    *,
    slots: Mapping[str, str],
    clarification: ClarificationContext | None,
) -> dict[str, str]:
    if clarification is None:
        return {}
    if tuple(clarification.missing_fields) != ASSET_SELECTOR_ALTERNATIVES:
        return {}
    if any(name in slots for name in _SLOT_FIELD_BY_NAME):
        return {}
    value = user_text.strip()
    if not value:
        return {}
    inferred = _infer_bare_selector(value)
    if inferred is None:
        raise SlotVerificationError("asset selector reply is not explicit enough")
    slot_name, canonical = inferred
    return {slot_name: canonical}


def _infer_bare_selector(value: str) -> tuple[str, str] | None:
    try:
        return "ip", _canonical_text(field=ClarificationField.IP, raw=value)
    except (SlotVerificationError, ValueError, ValidationError):
        pass
    if "." not in value:
        return None
    try:
        return "hostname", _canonical_text(field=ClarificationField.HOSTNAME, raw=value)
    except (SlotVerificationError, ValueError, ValidationError):
        return None


def _params_from_confirmed_slots(
    confirmed: tuple[ConfirmedSlot, ...],
) -> AssetLookupParams:
    payload: dict[str, object] = {}
    for slot in confirmed:
        attr = _PARAM_FIELD_BY_SLOT.get(slot.field)
        if attr is None:
            raise SlotVerificationError("confirmed slot field is not allowed")
        value = slot.value
        if not isinstance(value, ConfirmedTextValue):
            raise SlotVerificationError("asset selector slot has wrong value type")
        payload[attr] = _canonical_text(field=slot.field, raw=value.text)
    return AssetLookupParams.model_validate(payload)


def _canonical_text(*, field: ClarificationField, raw: str) -> str:
    if field is ClarificationField.ASSET_ID:
        return _ASSET_ID_ADAPTER.validate_python(raw)
    if field is ClarificationField.HOSTNAME:
        return _HOSTNAME_ADAPTER.validate_python(raw)
    if field is ClarificationField.IP:
        return _IP_ADAPTER.validate_python(raw)
    raise SlotVerificationError("asset selector field is not allowed")


def _validate_asset_target(
    *,
    target: ResolvedTarget,
    selector_kind: str,
    value: str,
) -> None:
    if (
        target.provider != ASSET_PROVIDER
        or target.resource_kind != ASSET_RESOURCE_KIND
        or target.selector_version != ASSET_SELECTOR_VERSION
        or target.resource_ids != (f"{selector_kind}:{value}",)
    ):
        raise SlotVerificationError("asset lookup target is incompatible")


def _invalid() -> SlotInvalid:
    return SlotInvalid(
        reason_code=InteractionRejectionReasonCode.CAPABILITY_FIELDS_INVALID
    )

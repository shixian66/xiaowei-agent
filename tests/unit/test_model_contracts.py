"""I1/RI3 模型窄口的严格 DTO 契约。"""

import json
import math

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    ClarificationContext,
    InteractionClassifierRequest,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelAdvisory,
    ModelInvocationProfile,
    ModelUsage,
    ProviderIntentResponse,
    ProviderIntentSlots,
    ProviderInteractionResponse,
    RouteSubject,
    SlowQueryAdvisoryRequest,
)


def test_interaction_classifier_request_has_only_current_text_and_clarification() -> None:
    parent = ClarificationContext(
        subject=RouteSubject(kind="route", proposed_kind=InteractionKind.UNKNOWN),
        confirmed_slots=(),
    )
    request = InteractionClassifierRequest(
        user_text="补充慢查询时间范围",
        clarification=parent,
    )

    assert set(type(request).model_fields) == {"user_text", "clarification"}
    assert request.clarification == parent
    with pytest.raises(ValidationError):
        InteractionClassifierRequest.model_validate(
            {
                "user_text": "检查慢查询",
                "history": ("must-not-exist",),
                "context_truncated": False,
            }
        )
    with pytest.raises(ValidationError):
        InteractionClassifierRequest(user_text="x" * 8_193, clarification=None)


def test_model_text_character_limit_implies_the_declared_utf8_ceiling() -> None:
    from xiaowei_agent.contracts.model import (
        MAX_MODEL_TEXT_BYTES,
        MAX_MODEL_TEXT_CHARACTERS,
    )

    exact = "\U0001f600" * MAX_MODEL_TEXT_CHARACTERS
    request = InteractionClassifierRequest(user_text=exact)

    assert len(request.user_text.encode("utf-8")) == MAX_MODEL_TEXT_BYTES
    assert MAX_MODEL_TEXT_BYTES == MAX_MODEL_TEXT_CHARACTERS * 4
    with pytest.raises(ValidationError):
        InteractionClassifierRequest(user_text=exact + "\U0001f600")


def test_model_text_rejects_unpaired_unicode_surrogate_as_validation_error() -> None:
    with pytest.raises(ValidationError):
        InteractionClassifierRequest(user_text="\ud800")


def test_advisory_text_character_limit_implies_its_utf8_ceiling() -> None:
    exact = "\U0001f600" * 16_384
    advisory = ModelAdvisory(analysis=exact, suggestions=(), uncertainties=())

    assert len(advisory.analysis.encode("utf-8")) == 64 * 1_024
    with pytest.raises(ValidationError):
        ModelAdvisory(analysis=exact + "\U0001f600", suggestions=(), uncertainties=())


def test_advisory_and_row_text_reject_unpaired_unicode_surrogates() -> None:
    with pytest.raises(ValidationError):
        ModelAdvisory(analysis="\ud800", suggestions=(), uncertainties=())
    for row in ({"\ud800": "x"}, {"queryId": "\ud800"}):
        with pytest.raises(ValidationError):
            SlowQueryAdvisoryRequest(rows=(row,), sampled=False)


def test_advisory_row_text_uses_the_same_character_derived_utf8_ceiling() -> None:
    exact = "\U0001f600" * 8_192
    request = SlowQueryAdvisoryRequest(rows=({exact: exact},), sampled=False)

    key, value = next(iter(request.rows[0].items()))
    assert len(key.encode("utf-8")) == 32 * 1_024
    assert isinstance(value, str)
    assert len(value.encode("utf-8")) == 32 * 1_024
    with pytest.raises(ValidationError):
        SlowQueryAdvisoryRequest(rows=({exact + "\U0001f600": "x"},), sampled=False)
    with pytest.raises(ValidationError):
        SlowQueryAdvisoryRequest(rows=({"queryId": exact + "\U0001f600"},), sampled=False)


def test_advisory_request_rejects_zero_or_twenty_one_rows() -> None:
    base = {"sampled": False}
    with pytest.raises(ValidationError):
        SlowQueryAdvisoryRequest(**base, rows=())
    with pytest.raises(ValidationError):
        SlowQueryAdvisoryRequest(**base, rows=({"queryId": "q"},) * 21)


def test_advisory_request_has_exact_rows_and_sampled_surface() -> None:
    request = SlowQueryAdvisoryRequest(rows=({"queryId": "q"},), sampled=False)

    assert set(type(request).model_fields) == {"rows", "sampled"}
    assert request.model_dump_json() == '{"rows":[{"queryId":"q"}],"sampled":false}'
    with pytest.raises(ValidationError):
        SlowQueryAdvisoryRequest(
            rows=({"queryId": "q"},),
            sampled=False,
            user_text="must not leave the process",
        )


def _advisory_row_for_serialized_size(size: int) -> dict[str, str]:
    row = {f"field_{index:02d}": "" for index in range(64)}
    empty_size = len(
        json.dumps(
            {"rows": [row], "sampled": False},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    remaining = size - empty_size
    for key in row:
        length = min(8_192, remaining)
        row[key] = "x" * length
        remaining -= length
    assert remaining == 0
    return row


def test_advisory_request_enforces_complete_serialized_byte_boundary() -> None:
    exact_row = _advisory_row_for_serialized_size(512 * 1_024)
    exact = SlowQueryAdvisoryRequest(rows=(exact_row,), sampled=False)

    assert len(exact.model_dump_json().encode("utf-8")) == 512 * 1_024
    last_key = next(reversed(exact_row))
    oversized_row = {**exact_row, last_key: exact_row[last_key] + "x"}
    with pytest.raises(ValidationError):
        SlowQueryAdvisoryRequest(rows=(oversized_row,), sampled=False)


@pytest.mark.parametrize(
    "field",
    ["source", "capability_id", "target", "sql", "approval", "tool", "next_steps"],
)
def test_provider_interaction_response_rejects_authority_and_source_fields(
    field: str,
) -> None:
    with pytest.raises(ValidationError):
        ProviderInteractionResponse.model_validate(
            {
                "proposed_kind": "conversation",
                "capability": None,
                "confidence": 0.5,
                field: "forbidden",
            }
        )


def test_provider_interaction_response_requires_capability_shape_to_match_kind() -> None:
    capability = {
        "intent": "starrocks.slow_query.diagnose",
        "slots": {"window_minutes": "30"},
        "missing": (),
        "confidence": 0.5,
    }
    accepted = ProviderInteractionResponse(
        proposed_kind=InteractionKind.CAPABILITY_REQUEST,
        capability=capability,
        confidence=0.5,
    )

    assert accepted.capability is not None
    with pytest.raises(ValidationError):
        ProviderInteractionResponse(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            capability=None,
            confidence=0.5,
        )
    with pytest.raises(ValidationError):
        ProviderInteractionResponse(
            proposed_kind=InteractionKind.CONVERSATION,
            capability=capability,
            confidence=0.5,
        )


def test_provider_intent_slots_have_a_fixed_sdk_compatible_wire_shape() -> None:
    from xiaowei_agent.contracts.intent import INTENT_SLOT_ALLOWLISTS

    expected = frozenset().union(*INTENT_SLOT_ALLOWLISTS.values())
    schema = ProviderIntentResponse.model_json_schema()
    slots_schema = schema["properties"]["slots"]
    slots_definition = schema["$defs"]["ProviderIntentSlots"]

    assert "$ref" in slots_schema
    assert frozenset(ProviderIntentSlots.model_fields) == expected
    assert set(slots_definition["properties"]) == expected
    assert slots_definition["additionalProperties"] is False
    assert "required" not in slots_definition
    assert all(
        property_schema.get("type") == "string"
        for property_schema in slots_definition["properties"].values()
    )


@pytest.mark.parametrize(
    ("intent", "slots"),
    [
        ("starrocks.slow_query.diagnose", {"window_minutes": None}),
        ("starrocks.slow_query.diagnose", {"asset_id": None}),
    ],
)
def test_provider_intent_response_rejects_explicit_null_slots(
    intent: str,
    slots: dict[str, None],
) -> None:
    with pytest.raises(ValidationError):
        ProviderIntentResponse(
            intent=intent,
            slots=slots,
            missing=(),
            confidence=0.5,
        )


def test_provider_intent_response_accepts_truly_omitted_optional_slots() -> None:
    response = ProviderIntentResponse(
        intent="starrocks.slow_query.diagnose",
        slots={},
        missing=(),
        confidence=0.5,
    )

    assert isinstance(response.slots, ProviderIntentSlots)
    assert response.slots.model_fields_set == set()
    assert response.slots.model_dump(exclude_none=True) == {}


@pytest.mark.parametrize(
    "field",
    [
        "capability",
        "Capability_ID",
        "\uff23\uff21\uff30\uff21\uff22\uff29\uff2c\uff29\uff34\uff39",
        "target-id",
        "SQL",
        "approvalState",
        "tool.name",
        "nextSteps",
        "policy_profile",
        "plan",
        "evidence",
        "task_status",
    ],
)
def test_provider_intent_response_rejects_authority_keys_inside_slots(field: str) -> None:
    with pytest.raises(ValidationError):
        ProviderIntentResponse.model_validate(
            {
                "intent": "starrocks.slow_query.diagnose",
                "slots": {field: "forbidden"},
                "missing": (),
                "confidence": 0.5,
            }
        )


@pytest.mark.parametrize(
    ("intent", "slots"),
    [
        (
            "starrocks.slow_query.diagnose",
            {
                "environment_id": "dev",
                "database": "sales",
                "user_name": "app",
                "query_id": "q1",
                "window_minutes": "30",
            },
        ),
        (
            "prometheus.alert.evidence",
            {
                "alert_name": "HostHighCpu",
                "instance": "node-1:9100",
                "fingerprint": "fp-1",
                "window_minutes": "30",
            },
        ),
        (
            "asset.inventory.lookup",
            {
                "asset_id": "asset-1",
                "hostname": "node-1.example.com",
                "ip": "192.0.2.1",
            },
        ),
        ("unknown", {}),
    ],
)
def test_provider_intent_response_accepts_current_intent_specific_slots(
    intent: str, slots: dict[str, str]
) -> None:
    response = ProviderIntentResponse(
        intent=intent, slots=slots, missing=(), confidence=0.5
    )

    assert response.slots.model_dump(exclude_none=True) == slots


@pytest.mark.parametrize(
    ("intent", "slot"),
    [
        ("starrocks.slow_query.diagnose", "asset_id"),
        ("prometheus.alert.evidence", "database"),
        ("asset.inventory.lookup", "window_minutes"),
        ("unknown", "hostname"),
    ],
)
def test_provider_intent_response_rejects_slots_from_another_intent(
    intent: str, slot: str
) -> None:
    with pytest.raises(ValidationError):
        ProviderIntentResponse(
            intent=intent,
            slots={slot: "forbidden"},
            missing=(),
            confidence=0.5,
        )


@pytest.mark.parametrize(
    ("intent", "missing"),
    [
        ("starrocks.slow_query.diagnose", "approval"),
        ("prometheus.alert.evidence", "SQL"),
        ("asset.inventory.lookup", "nextSteps"),
        ("unknown", "tool.name"),
    ],
)
def test_provider_intent_response_rejects_authority_keys_inside_missing(
    intent: str, missing: str
) -> None:
    with pytest.raises(ValidationError):
        ProviderIntentResponse(
            intent=intent,
            slots={},
            missing=(missing,),
            confidence=0.5,
        )


@pytest.mark.parametrize(
    ("intent", "missing"),
    [
        ("starrocks.slow_query.diagnose", ("environment_id",)),
        ("prometheus.alert.evidence", ("alert_name", "instance")),
        ("asset.inventory.lookup", ("asset_selector",)),
        ("unknown", ()),
    ],
)
def test_provider_intent_response_accepts_current_missing_fields(
    intent: str, missing: tuple[str, ...]
) -> None:
    response = ProviderIntentResponse(
        intent=intent,
        slots={},
        missing=missing,
        confidence=0.5,
    )

    assert response.missing == missing


def test_model_advisory_rejects_authority_fields_and_long_text() -> None:
    base = {"analysis": "分析", "suggestions": (), "uncertainties": ()}
    with pytest.raises(ValidationError):
        ModelAdvisory.model_validate({**base, "next_steps": ("执行",)})
    with pytest.raises(ValidationError):
        ModelAdvisory.model_validate({**base, "analysis": "x" * 16_385})


@pytest.mark.parametrize("confidence", [True, math.nan, math.inf, -math.inf, -0.1, 1.1])
def test_provider_responses_reject_invalid_confidence(confidence: object) -> None:
    with pytest.raises(ValidationError):
        ProviderIntentResponse.model_validate(
            {
                "intent": "starrocks.slow_query.diagnose",
                "slots": {"window_minutes": "30"},
                "missing": (),
                "confidence": confidence,
            }
        )
    with pytest.raises(ValidationError):
        ProviderInteractionResponse.model_validate(
            {
                "proposed_kind": "conversation",
                "capability": None,
                "confidence": confidence,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", True),
        ("input_tokens", 1.0),
        ("input_tokens", -1),
        ("input_tokens", 2**63),
        ("output_tokens", True),
        ("output_tokens", 1.0),
        ("output_tokens", -1),
        ("output_tokens", 2**63),
    ],
)
def test_model_usage_rejects_non_integer_negative_and_overflow_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ModelUsage.model_validate(
            {"input_tokens": None, "output_tokens": None, field: value}
        )


def test_model_usage_accepts_nullable_and_signed_64_bit_boundary() -> None:
    assert ModelUsage(input_tokens=None, output_tokens=None).model_dump() == {
        "input_tokens": None,
        "output_tokens": None,
    }
    exact = ModelUsage(input_tokens=2**63 - 1, output_tokens=0)
    assert exact.input_tokens == 2**63 - 1
    assert exact.output_tokens == 0


def test_model_result_wrappers_and_invocation_profile_are_closed_and_immutable() -> None:
    usage = ModelUsage(input_tokens=12, output_tokens=4)
    interaction = InteractionModelResult(
        draft=InteractionDraft(
            proposed_kind=InteractionKind.CONVERSATION,
            capability_draft=None,
            confidence=0.5,
            source=InteractionSource.MODEL,
        ),
        usage=usage,
    )
    advisory = AdvisoryModelResult(
        advisory=ModelAdvisory(analysis="分析", suggestions=(), uncertainties=()),
        usage=usage,
    )
    profile = ModelInvocationProfile()

    assert set(type(interaction).model_fields) == {"draft", "usage"}
    assert set(type(advisory).model_fields) == {"advisory", "usage"}
    assert profile.model_dump() == {
        "provider": "google-gemini-developer-api",
        "model": "gemini-3-flash-preview",
        "api_version": "v1beta",
        "origin": "https://generativelanguage.googleapis.com",
        "interaction_prompt_revision": "i1-interaction-prompt-v1",
        "interaction_schema_revision": "i1-interaction-schema-v1",
        "advisory_prompt_revision": "ri3-advisory-prompt-v1",
        "advisory_schema_revision": "ri3-advisory-schema-v1",
        "interaction_thinking_level": "LOW",
        "advisory_thinking_level": "HIGH",
        "interaction_timeout_seconds": 60,
        "advisory_timeout_seconds": 180,
        "interaction_output_tokens": 2_048,
        "advisory_output_tokens": 4_000,
    }
    with pytest.raises(ValidationError):
        ModelInvocationProfile.model_validate({"provider": "other"})
    with pytest.raises(ValidationError):
        ModelInvocationProfile.model_validate({"interaction_timeout_seconds": 60.0})
    with pytest.raises(ValidationError):
        ModelInvocationProfile.model_validate({"extra": "forbidden"})

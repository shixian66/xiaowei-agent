"""RI3 模型窄口的严格 DTO 契约。"""

import json
import math
from importlib.util import find_spec

import pytest
from pydantic import ValidationError

_MODEL_MODULE = "xiaowei_agent.contracts.model"


def _types() -> tuple[type[object], type[object]]:
    if find_spec(_MODEL_MODULE) is None:
        pytest.fail("model contract module is missing")
    from xiaowei_agent.contracts import ModelIntentRequest, ProviderIntentResponse

    return ModelIntentRequest, ProviderIntentResponse


def _model_types() -> tuple[type[object], type[object], type[object], type[object]]:
    if find_spec(_MODEL_MODULE) is None:
        pytest.fail("model contract module is missing")
    from xiaowei_agent.contracts import (
        ModelAdvisory,
        ModelIntentRequest,
        ProviderIntentResponse,
        SlowQueryAdvisoryRequest,
    )

    return (
        ModelIntentRequest,
        SlowQueryAdvisoryRequest,
        ProviderIntentResponse,
        ModelAdvisory,
    )


def test_intent_request_is_strict_immutable_and_bounded() -> None:
    model_intent_request, _ = _types()
    request = model_intent_request(
        user_text="检查慢查询",
        history=("上一轮结果",),
        context_truncated=False,
    )

    with pytest.raises(ValidationError):
        request.model_copy(update={"context_truncated": 0})
    with pytest.raises(ValidationError):
        model_intent_request.model_validate(
            {
                "user_text": "检查慢查询",
                "history": (),
                "context_truncated": False,
                "prompt": "untrusted override",
            }
        )
    with pytest.raises(ValidationError):
        model_intent_request(
            user_text="x" * 8193,
            history=(),
            context_truncated=False,
        )


def test_intent_request_rejects_more_than_twenty_history_items_and_bool_flag_spoof() -> None:
    model_intent_request, _, _, _ = _model_types()
    with pytest.raises(ValidationError):
        model_intent_request(user_text="x", history=("h",) * 21, context_truncated=False)
    with pytest.raises(ValidationError):
        model_intent_request(user_text="x", history=(), context_truncated=0)


def test_intent_request_enforces_history_aggregate_character_boundary() -> None:
    model_intent_request, _, _, _ = _model_types()
    exact_history = ("x" * 8_000,) * 8
    request = model_intent_request(
        user_text="x", history=exact_history, context_truncated=False
    )

    assert sum(map(len, request.history)) == 64_000
    with pytest.raises(ValidationError):
        model_intent_request(
            user_text="x",
            history=(*exact_history, "x"),
            context_truncated=False,
        )


def test_model_text_accepts_exact_multibyte_boundary_and_rejects_excess() -> None:
    model_intent_request, _, _, _ = _model_types()
    exact = "\U0001f600" * 8_192

    request = model_intent_request(
        user_text=exact,
        history=(),
        context_truncated=False,
    )

    assert len(request.user_text.encode("utf-8")) == 32 * 1_024
    with pytest.raises(ValidationError):
        model_intent_request(
            user_text=exact + "\U0001f600",
            history=(),
            context_truncated=False,
        )


def test_model_text_rejects_unpaired_unicode_surrogate_as_validation_error() -> None:
    model_intent_request, _, _, _ = _model_types()

    with pytest.raises(ValidationError):
        model_intent_request(
            user_text="\ud800",
            history=(),
            context_truncated=False,
        )


def test_advisory_request_rejects_zero_or_twenty_one_rows() -> None:
    _, slow_query_advisory_request, _, _ = _model_types()
    base = {"sampled": False}
    with pytest.raises(ValidationError):
        slow_query_advisory_request(**base, rows=())
    with pytest.raises(ValidationError):
        slow_query_advisory_request(**base, rows=({"queryId": "q"},) * 21)


def test_advisory_request_has_exact_rows_and_sampled_surface() -> None:
    _, slow_query_advisory_request, _, _ = _model_types()

    request = slow_query_advisory_request(rows=({"queryId": "q"},), sampled=False)

    assert set(type(request).model_fields) == {"rows", "sampled"}
    assert request.model_dump_json() == '{"rows":[{"queryId":"q"}],"sampled":false}'
    with pytest.raises(ValidationError):
        slow_query_advisory_request(
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
    _, slow_query_advisory_request, _, _ = _model_types()
    exact_row = _advisory_row_for_serialized_size(512 * 1_024)

    exact = slow_query_advisory_request(rows=(exact_row,), sampled=False)

    assert len(exact.model_dump_json().encode("utf-8")) == 512 * 1_024
    last_key = next(reversed(exact_row))
    oversized_row = {**exact_row, last_key: exact_row[last_key] + "x"}
    with pytest.raises(ValidationError):
        slow_query_advisory_request(rows=(oversized_row,), sampled=False)


@pytest.mark.parametrize(
    "field",
    ["source", "capability_id", "target", "sql", "approval", "tool", "next_steps"],
)
def test_provider_intent_response_rejects_authority_and_source_fields(field: str) -> None:
    _, _, provider_intent_response, _ = _model_types()
    payload = {
        "intent": "slow_query",
        "slots": {},
        "missing": (),
        "confidence": 0.5,
        field: "forbidden",
    }
    with pytest.raises(ValidationError):
        provider_intent_response.model_validate(payload)


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
def test_provider_intent_response_rejects_authority_keys_inside_slots(
    field: str,
) -> None:
    _, _, provider_intent_response, _ = _model_types()

    with pytest.raises(ValidationError):
        provider_intent_response.model_validate(
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
    _, _, provider_intent_response, _ = _model_types()

    response = provider_intent_response(
        intent=intent, slots=slots, missing=(), confidence=0.5
    )

    assert dict(response.slots) == slots


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
    _, _, provider_intent_response, _ = _model_types()

    with pytest.raises(ValidationError):
        provider_intent_response(
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
    _, _, provider_intent_response, _ = _model_types()

    with pytest.raises(ValidationError):
        provider_intent_response(
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
    _, _, provider_intent_response, _ = _model_types()

    response = provider_intent_response(
        intent=intent,
        slots={},
        missing=missing,
        confidence=0.5,
    )

    assert response.missing == missing


def test_model_advisory_rejects_authority_fields_and_long_text() -> None:
    _, _, _, model_advisory = _model_types()
    base = {"analysis": "分析", "suggestions": (), "uncertainties": ()}
    with pytest.raises(ValidationError):
        model_advisory.model_validate({**base, "next_steps": ("执行",)})
    with pytest.raises(ValidationError):
        model_advisory.model_validate({**base, "analysis": "x" * 16_385})


@pytest.mark.parametrize("confidence", [True, math.nan, math.inf, -math.inf])
def test_provider_intent_response_rejects_non_finite_or_non_numeric_confidence(
    confidence: object,
) -> None:
    _, provider_intent_response = _types()
    with pytest.raises(ValidationError):
        provider_intent_response.model_validate(
            {
                "intent": "slow_query",
                "slots": {},
                "missing": (),
                "confidence": confidence,
            }
        )

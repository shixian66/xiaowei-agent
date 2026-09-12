"""RI3 模型窄口的严格 DTO 契约。"""

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


def test_advisory_request_rejects_zero_or_twenty_one_rows() -> None:
    _, slow_query_advisory_request, _, _ = _model_types()
    base = {"user_text": "x", "history": (), "context_truncated": False, "sampled": False}
    with pytest.raises(ValidationError):
        slow_query_advisory_request(**base, rows=())
    with pytest.raises(ValidationError):
        slow_query_advisory_request(**base, rows=({"queryId": "q"},) * 21)


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

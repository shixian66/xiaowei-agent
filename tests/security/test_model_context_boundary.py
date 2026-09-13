"""模型只看窄 DTO，也不能把文本变成执行、目标或持久化权力。"""

import datetime as dt
import inspect

import pytest
from pydantic import ValidationError
from tests.fakes.admission import CONTEXT, slow_query_plan
from tests.fakes.recordings import _row

from xiaowei_agent.application.model_advisory import (
    project_slow_query_advisory_request,
)
from xiaowei_agent.application.model_intent import build_model_intent_request
from xiaowei_agent.application.model_ports import (
    MODEL_ERROR_FALLBACK_CODES,
    IntentModelPort,
    SlowQueryAdvisoryPort,
    fallback_code_for_model_error,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.capabilities.target import resolve_target
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    ExternalSource,
    IntentDraft,
    IntentSource,
    ModelAdvisory,
    ModelErrorCode,
    ModelFallbackCode,
    ModelIntentRequest,
    SlowQueryAdvisoryRequest,
    TaskOutcome,
    TaskStatus,
)
from xiaowei_agent.persistence.model_artifacts import (
    AdvisoryArtifactCandidate,
    IntentArtifactCandidate,
)
from xiaowei_agent.persistence.schema import (
    TASK_ACCEPTED_INTENTS,
    TASK_MODEL_ADVISORIES,
)
from xiaowei_agent.redaction import scrub_text

pytestmark = pytest.mark.security


def test_every_model_error_has_one_explicit_total_fallback_mapping() -> None:
    assert set(MODEL_ERROR_FALLBACK_CODES) == set(ModelErrorCode)
    assert set(MODEL_ERROR_FALLBACK_CODES.values()) <= set(ModelFallbackCode)
    assert (
        MODEL_ERROR_FALLBACK_CODES[ModelErrorCode.TIMEOUT]
        is ModelFallbackCode.PROVIDER_TIMEOUT
    )
    assert fallback_code_for_model_error(object()) is ModelFallbackCode.UNAVAILABLE


def test_model_ports_expose_only_the_two_narrow_typed_calls() -> None:
    intent_calls = {
        name
        for name, value in IntentModelPort.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    advisory_calls = {
        name
        for name, value in SlowQueryAdvisoryPort.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert intent_calls == {"generate_intent"}
    assert advisory_calls == {"generate_advisory"}
    assert set(inspect.signature(IntentModelPort.generate_intent).parameters) == {
        "self",
        "request",
    }
    assert set(
        inspect.signature(SlowQueryAdvisoryPort.generate_advisory).parameters
    ) == {"self", "request", "max_output_tokens"}


def test_intent_request_has_no_identity_policy_tool_or_endpoint_context() -> None:
    assert set(ModelIntentRequest.model_fields) == {
        "user_text",
        "history",
        "context_truncated",
    }
    request = build_model_intent_request(
        user_text="检查慢查询",
        history=("上一轮只读结果",),
    )
    dumped = request.model_dump(mode="python")
    forbidden = {
        "tenant_id",
        "actor",
        "environment_id",
        "trace_id",
        "policy_revision",
        "tools",
        "endpoint",
        "api_key",
    }
    assert forbidden.isdisjoint(dumped)


def test_secret_shaped_input_is_scrubbed_before_it_enters_the_model_dto() -> None:
    secret = "token=" + "synthetic-value"
    user_text = "检查慢查询 " + secret
    history_text = "上一轮 " + secret
    request = build_model_intent_request(
        user_text=user_text,
        history=(history_text,),
    )

    assert secret not in request.model_dump_json()
    assert request.user_text == scrub_text(user_text) != user_text
    assert request.history[0] == scrub_text(history_text) != history_text


def test_model_candidate_generation_cannot_be_changed_by_confidence_or_slots() -> None:
    resolver = DeterministicCapabilityResolver()
    snapshot = StaticCapabilityRegistry().snapshot()
    plain = IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={},
        missing=(),
        confidence=0.01,
        source=IntentSource.MODEL,
    )
    suggestive = IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"environment_id": "prod", "window_minutes": "30"},
        missing=(),
        confidence=1.0,
        source=IntentSource.MODEL,
    )

    assert resolver.resolve(
        draft=plain, context=CONTEXT, snapshot=snapshot
    ) == resolver.resolve(draft=suggestive, context=CONTEXT, snapshot=snapshot)
    assert resolve_target(context=CONTEXT, draft=suggestive).environment_id == "dev"


def test_advisory_generic_dto_does_not_replace_the_capability_projector() -> None:
    row = _row(0, query_time=12_000) | {"stmt": "SELECT synthetic"}
    assert SlowQueryAdvisoryRequest(rows=(row,), sampled=False)
    evidence = EvidenceEnvelope(
        evidence_id="task-model-boundary:s1",
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        facts=(row,),
        source="starrocks-fake",
        source_kind=ExternalSource.TOOL,
        captured_at=dt.datetime(2026, 9, 13, tzinfo=dt.UTC),
        sampled=False,
        limitations=(),
    )
    outcome = TaskOutcome(
        task_id="task-model-boundary",
        status=TaskStatus.SUCCEEDED,
        terminal_reason=None,
        evidence_refs=(evidence.evidence_id,),
        render_ref=None,
    )
    verdict = AnswerabilityVerdict(
        sufficient=True,
        limitations=(),
        missing=(),
        downgrade_suggestion=False,
        needs_user_input=False,
    )

    assert project_slow_query_advisory_request(
        task_id=outcome.task_id,
        plan=slow_query_plan(),
        outcome=outcome,
        evidences=(evidence,),
        verdict=verdict,
    ) is None


@pytest.mark.parametrize(
    "field",
    ["status", "facts", "refs", "next_steps", "sql", "tool_call", "action"],
)
def test_advisory_cannot_carry_deterministic_facts_or_actions(field: str) -> None:
    with pytest.raises(ValidationError):
        ModelAdvisory.model_validate(
            {
                "analysis": "合成分析",
                "suggestions": [],
                "uncertainties": [],
                field: "forbidden",
            }
        )


def test_model_artifact_payloads_and_tables_exclude_raw_provider_material() -> None:
    expected_intent = {
        "draft",
        "origin",
        "provider",
        "model",
        "provider_origin",
        "prompt_revision",
        "schema_revision",
        "input_digest",
        "result_digest",
        "usage",
    }
    expected_advisory = {
        "advisory",
        "origin",
        "provider",
        "model",
        "provider_origin",
        "prompt_revision",
        "schema_revision",
        "input_digest",
        "result_digest",
        "usage",
    }
    common_columns = {
        "task_id",
        "artifact_version",
        "origin",
        "provider",
        "model",
        "provider_origin",
        "prompt_revision",
        "schema_revision",
        "input_digest",
        "result_digest",
        "usage",
        "created_at",
        "fencing_token",
    }

    assert set(IntentArtifactCandidate.model_fields) == expected_intent
    assert set(AdvisoryArtifactCandidate.model_fields) == expected_advisory
    assert set(TASK_ACCEPTED_INTENTS.c.keys()) == common_columns | {"draft"}
    assert set(TASK_MODEL_ADVISORIES.c.keys()) == common_columns | {"advisory"}
    forbidden = {
        "prompt",
        "raw_request",
        "raw_response",
        "credential",
        "api_key",
        "secret_path",
        "provider_error",
        "evidence",
    }
    assert forbidden.isdisjoint(TASK_ACCEPTED_INTENTS.c.keys())
    assert forbidden.isdisjoint(TASK_MODEL_ADVISORIES.c.keys())

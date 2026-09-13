"""RI3 小型离线 eval：逐场景报告，安全边界必须逐条通过。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.fakes.admission import CONTEXT, slow_query_plan
from tests.fakes.recordings import _row

from xiaowei_agent.application.model_advisory import (
    project_slow_query_advisory_request,
)
from xiaowei_agent.application.model_intent import request_intent_draft
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    AnswerabilityVerdict,
    EvidenceEnvelope,
    ExternalSource,
    IntentDraft,
    IntentModelResult,
    IntentSource,
    ModelAdvisory,
    ModelFallbackCode,
    ModelIntentRequest,
    ModelInvocationProfile,
    ModelUsage,
    TaskOutcome,
    TaskStatus,
)

_CORPUS_DIR = Path(__file__).parent / "corpus"
_INTENT = json.loads((_CORPUS_DIR / "ri3_intent.json").read_text(encoding="utf-8"))
_ADVISORY = json.loads(
    (_CORPUS_DIR / "ri3_slow_query_advisory.json").read_text(encoding="utf-8")
)
_SNAPSHOT = StaticCapabilityRegistry().snapshot()


class _IntentPort:
    def __init__(self, draft: IntentDraft) -> None:
        self._draft = draft

    async def generate_intent(self, request: ModelIntentRequest) -> IntentModelResult:
        del request
        return IntentModelResult(draft=self._draft, usage=ModelUsage())


def _rule_fallback() -> IntentDraft:
    return IntentDraft(
        intent="unknown",
        slots={},
        missing=(),
        confidence=0.0,
        source=IntentSource.USER,
    )


def _verdict(*, sufficient: bool = True) -> AnswerabilityVerdict:
    return AnswerabilityVerdict(
        sufficient=sufficient,
        limitations=() if sufficient else ("synthetic gap",),
        missing=(),
        downgrade_suggestion=not sufficient,
        needs_user_input=False,
    )


def _outcome(*, status: TaskStatus = TaskStatus.SUCCEEDED) -> TaskOutcome:
    return TaskOutcome(
        task_id="task-ri3-eval",
        status=status,
        terminal_reason=None,
        evidence_refs=("task-ri3-eval:s1",),
        render_ref=None,
    )


def _evidence(
    row: dict[str, object], *, source_kind: ExternalSource = ExternalSource.TOOL
) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id="task-ri3-eval:s1",
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        facts=(row,),
        source="starrocks-fake",
        source_kind=source_kind,
        captured_at=dt.datetime(2026, 9, 13, tzinfo=dt.UTC),
        sampled=False,
        limitations=(),
    )


def test_ri3_eval_corpus_shape_is_fixed() -> None:
    assert _INTENT["version"] == _ADVISORY["version"] == 1
    assert len(_INTENT["cases"]) == 10
    assert len(_ADVISORY["cases"]) == 5
    assert len({case["id"] for case in _INTENT["cases"]}) == 10
    assert len({case["id"] for case in _ADVISORY["cases"]}) == 5


@pytest.mark.parametrize("case", _INTENT["cases"], ids=lambda case: case["id"])
async def test_each_intent_case_has_its_own_candidate_result(
    case: dict[str, Any],
) -> None:
    proposed = IntentDraft(
        intent=case["intent"],
        slots=case["slots"],
        missing=(),
        confidence=0.8,
        source=IntentSource.MODEL,
    )
    stage = await request_intent_draft(
        request=ModelIntentRequest(
            user_text="合成离线请求", history=(), context_truncated=False
        ),
        model=_IntentPort(proposed),
        profile=ModelInvocationProfile(),
        fallback=_rule_fallback,
    )
    candidates = DeterministicCapabilityResolver().resolve(
        draft=stage.draft,
        context=CONTEXT,
        snapshot=_SNAPSHOT,
    )

    assert (stage.observation.fallback_code is None) is case["accepted"]
    assert sorted({item.capability_id for item in candidates.items}) == sorted(
        case["expected_capabilities"]
    )


@pytest.mark.parametrize(
    "field", _INTENT["authority_fields"], ids=lambda field: f"intent-{field}"
)
def test_intent_authority_fields_are_unrepresentable(field: str) -> None:
    with pytest.raises(ValidationError):
        IntentDraft.model_validate(
            {
                "intent": "starrocks.slow_query.diagnose",
                "slots": {},
                "missing": [],
                "confidence": 0.8,
                "source": "model",
                field: "forbidden",
            }
        )


@pytest.mark.parametrize(
    "slot", _INTENT["authority_slots"], ids=lambda slot: f"slot-{slot}"
)
async def test_intent_authority_slots_fail_the_application_recheck(slot: str) -> None:
    proposed = IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={slot: "forbidden"},
        missing=(),
        confidence=0.8,
        source=IntentSource.MODEL,
    )
    stage = await request_intent_draft(
        request=ModelIntentRequest(
            user_text="合成离线请求", history=(), context_truncated=False
        ),
        model=_IntentPort(proposed),
        profile=ModelInvocationProfile(),
        fallback=_rule_fallback,
    )

    assert stage.draft == _rule_fallback()
    assert stage.observation.fallback_code is ModelFallbackCode.INVALID_RESPONSE


@pytest.mark.parametrize("case", _ADVISORY["cases"], ids=lambda case: case["id"])
def test_each_advisory_case_has_its_own_projection_result(
    case: dict[str, str],
) -> None:
    row = _row(0, query_time=12_000)
    status = TaskStatus.SUCCEEDED
    verdict = _verdict()
    source = ExternalSource.TOOL
    if case["scenario"] == "failed":
        status = TaskStatus.FAILED
    elif case["scenario"] == "insufficient":
        verdict = _verdict(sufficient=False)
    elif case["scenario"] == "wrong_source":
        source = ExternalSource.USER
    elif case["scenario"] == "sensitive_extra":
        row["stmt"] = "SELECT synthetic"

    projected = project_slow_query_advisory_request(
        task_id="task-ri3-eval",
        plan=slow_query_plan(),
        outcome=_outcome(status=status),
        evidences=(_evidence(row, source_kind=source),),
        verdict=verdict,
    )

    assert (projected is not None) is (case["expected"] == "request")


@pytest.mark.parametrize(
    "field", _ADVISORY["authority_fields"], ids=lambda field: f"advisory-{field}"
)
def test_advisory_cannot_encode_facts_state_or_actions(field: str) -> None:
    with pytest.raises(ValidationError):
        ModelAdvisory.model_validate(
            {
                "analysis": "合成分析",
                "suggestions": [],
                "uncertainties": [],
                field: "forbidden",
            }
        )


def test_advisory_wrapper_cannot_add_authority_around_a_valid_advisory() -> None:
    valid = AdvisoryModelResult(
        advisory=ModelAdvisory(
            analysis="合成分析", suggestions=(), uncertainties=()
        ),
        usage=ModelUsage(),
    )
    with pytest.raises(ValidationError):
        AdvisoryModelResult.model_validate(
            valid.model_dump(mode="python") | {"next_steps": ["execute"]}
        )

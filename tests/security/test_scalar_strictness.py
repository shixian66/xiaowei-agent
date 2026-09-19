"""跨边界标量不接受隐式转换。

Pydantic 默认 lax 模式会把 `"yes"` / `1` 收成 `bool`、`"2"` / `True` 收成 `int`、
`bytes` 收成 `str`。一个被构造成 ``allow="yes"`` 的 PolicyDecision 会在 Gateway
处**真的放行**——这不是理论风险。

严格性由 ``Contract`` 的 ``model_config`` 统一提供（默认严格，而非逐字段选择加入）；
逐字段开启必然会漏，本项目已经漏过一轮。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AgentError,
    AnswerabilityVerdict,
    Candidate,
    EffectClass,
    ErrorCategory,
    EvidenceEnvelope,
    ExternalContent,
    ExternalSource,
    IntentDraft,
    IntentSource,
    OperationSpec,
    PlanStep,
    PolicyDecision,
    ReadClass,
    RequestEnvelope,
    RiskLevel,
    ToolCall,
)

pytestmark = pytest.mark.security

_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
_TRUTHY = ["yes", "1", "on", 1]


@pytest.mark.parametrize("sneaky", _TRUTHY)
def test_policy_decision_allow_rejects_truthy_strings(sneaky: object) -> None:
    """最关键的一条：allow 被 lax 收成 True 会让 Gateway 真的放行。"""
    with pytest.raises(ValidationError):
        PolicyDecision(
            allow=sneaky,
            reason_code="x",
            risk=RiskLevel.LOW,
            policy_revision="r",
            obligations=(),
        )


@pytest.mark.parametrize("sneaky", _TRUTHY)
def test_answerability_flags_reject_truthy_strings(sneaky: object) -> None:
    with pytest.raises(ValidationError):
        AnswerabilityVerdict(
            sufficient=sneaky,
            limitations=(),
            missing=(),
            downgrade_suggestion=False,
            needs_user_input=False,
        )


@pytest.mark.parametrize("sneaky", _TRUTHY)
def test_operation_spec_side_effect_rejects_truthy_strings(sneaky: object) -> None:
    with pytest.raises(ValidationError):
        OperationSpec(
            operation="o",
            gateway="g",
            effect_class=EffectClass.MUTATE_TARGET,
            read_class=None,
            side_effect=sneaky,
            argument_schema_ref="s",
        )


@pytest.mark.parametrize("sneaky", _TRUTHY)
def test_plan_step_side_effect_rejects_truthy_strings(sneaky: object) -> None:
    with pytest.raises(ValidationError):
        PlanStep(
            step_id="s",
            operation="o",
            typed_arguments={},
            depends_on=(),
            side_effect=sneaky,
            effect_class=EffectClass.READ,
            read_class=ReadClass.BOUNDED,
        )


@pytest.mark.parametrize("sneaky", _TRUTHY)
def test_agent_error_retryable_rejects_truthy_strings(sneaky: object) -> None:
    with pytest.raises(ValidationError):
        AgentError(
            code="c",
            category=ErrorCategory.UPSTREAM,
            retryable=sneaky,
            message_key="k",
        )


def _evidence(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "evidence_id": "e",
        "capability_id": "c",
        "capability_version": "1.0.0",
        "facts": (),
        "source": "s",
        "source_kind": ExternalSource.TOOL,
        "captured_at": _AT,
        "readonly": True,
        "sampled": True,
        "limitations": (),
    }
    return base | overrides


@pytest.mark.parametrize("sneaky", _TRUTHY)
def test_evidence_sampled_rejects_truthy_strings(sneaky: object) -> None:
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**_evidence(sampled=sneaky))


def test_evidence_readonly_rejects_truthy_one() -> None:
    """Literal[True] 在 lax 下会接受 1；严格模式必须要求真正的 True。"""
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**_evidence(readonly=1))


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (
            RequestEnvelope,
            {
                "request_id": "r",
                "tenant_id": "t",
                "actor": "a",
                "channel": "cli",
                "text": b"hello",
                "idempotency_key": "k",
            },
        ),
        (
            ExternalContent,
            {
                "source": ExternalSource.TOOL,
                "content": b"err",
                "digest": "0" * 64,
                "captured_at": _AT,
            },
        ),
    ],
    ids=["RequestEnvelope.text", "ExternalContent.content"],
)
def test_str_fields_reject_bytes(factory: type, kwargs: dict[str, object]) -> None:
    """lax 模式下 str 会接受并解码 bytes，使文本字段能被二进制内容填充。"""
    with pytest.raises(ValidationError):
        factory(**kwargs)


def test_typed_arguments_reject_bytes() -> None:
    with pytest.raises(ValidationError):
        PlanStep(
            step_id="s",
            operation="o",
            typed_arguments={"x": b"abc"},
            depends_on=(),
            side_effect=False,
            effect_class=EffectClass.READ,
            read_class=ReadClass.BOUNDED,
        )


def test_tool_call_typed_args_reject_bytes() -> None:
    with pytest.raises(ValidationError):
        ToolCall(
            gateway="g",
            operation="o",
            step_id="s",
            typed_args={"x": b"abc"},
            timeout_seconds=1.0,
            idempotency_key="k",
        )


def test_evidence_facts_reject_bytes() -> None:
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**_evidence(facts=({"x": b"abc"},)))


def test_confidence_rejects_numeric_strings() -> None:
    with pytest.raises(ValidationError):
        IntentDraft(
            intent="i",
            slots={},
            missing=(),
            confidence="0.9",
            source=IntentSource.MODEL,
        )


def test_score_rejects_bool() -> None:
    """bool 是 int 的子类；lax 下 True 会被当成 1.0 通过 le=1.0。"""
    with pytest.raises(ValidationError):
        Candidate(
            capability_id="c",
            capability_version="1.0.0",
            operation="o",
            score=True,
            match_evidence=(),
            required_context=(),
        )

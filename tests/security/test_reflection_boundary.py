"""AnswerabilityVerdict 是 Reflection 的唯一输出契约。

**契约层面不存在让 Reflection 修改计划或写 TaskStore 的入口**：字段集只有五项且
extra="forbid"，越权建议根本无法被表达，而不是被表达后再拒绝（ARCHITECTURE §4.2）。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    ExternalSource,
    MissingItem,
)

pytestmark = pytest.mark.security

_FORBIDDEN = [
    "steps",
    "step_id",
    "add_step",
    "next_step",
    "plan",
    "plan_hash",
    "tool",
    "tool_call",
    "tool_call_hash",
    "gateway",
    "adapter",
    "operation",
    "target",
    "target_fingerprint",
    "resource_ids",
    "selector",
    "environment_id",
    "tenant_id",
    "actor",
    "permission",
    "effect_class",
    "side_effect",
    "approved",
    "approval_ref",
    "sql",
    "query",
    "task_status",
    "terminal_reason",
    "policy_revision",
]

_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)


def _verdict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sufficient": True,
        "limitations": (),
        "missing": (),
        "downgrade_suggestion": False,
        "needs_user_input": False,
    }
    return base | overrides


def test_field_set_is_exactly_five() -> None:
    assert set(AnswerabilityVerdict.model_fields) == {
        "sufficient",
        "limitations",
        "missing",
        "downgrade_suggestion",
        "needs_user_input",
    }


@pytest.mark.parametrize("field", _FORBIDDEN)
def test_execution_authority_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        AnswerabilityVerdict(**_verdict(**{field: "x"}))


def test_verdict_is_immutable() -> None:
    verdict = AnswerabilityVerdict(**_verdict())
    with pytest.raises(ValidationError):
        verdict.sufficient = False  # type: ignore[misc]


def test_missing_items_carry_only_keys_and_reason_codes() -> None:
    """缺失项不得携带取数指令。"""
    assert set(MissingItem.model_fields) == {"key", "reason_key"}


def _evidence(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "evidence_id": "e1",
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "facts": ({"query_id": "q1"},),
        "source": "starrocks-fake",
        "source_kind": ExternalSource.TOOL,
        "captured_at": _AT,
        "readonly": True,
        "sampled": True,
        "limitations": ("sampled window: 30m",),
        "redaction_ref": None,
    }
    return base | overrides


def test_evidence_is_always_readonly_in_m2_to_m7() -> None:
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**_evidence(readonly=False))


def test_evidence_facts_are_deeply_immutable() -> None:
    envelope = EvidenceEnvelope(**_evidence())
    with pytest.raises(TypeError):
        envelope.facts[0]["query_id"] = "tampered"  # type: ignore[index]


@pytest.mark.parametrize("field", ["sampled", "limitations"])
def test_evidence_requires_explicit_sampling_and_limitations(field: str) -> None:
    """样本性与限制是必填：不能靠调用方记得填。"""
    payload = _evidence()
    del payload[field]
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**payload)


def test_copy_cannot_make_evidence_writable() -> None:
    envelope = EvidenceEnvelope(**_evidence())
    with pytest.raises(ValidationError):
        envelope.model_copy(update={"readonly": False})

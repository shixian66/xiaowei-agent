"""IntentDraft 是不可信度最高的 DTO：不得携带任何执行决策字段。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext

pytestmark = pytest.mark.security

_FORBIDDEN = [
    "capability_id",
    "capability_version",
    "operation",
    "side_effect",
    "effect_class",
    "sql",
    "target",
    "resource_ids",
    "policy_revision",
    "approved",
    "plan_hash",
    "tool_call_hash",
    "environment_id",
    "tenant_id",
    "actor",
]


def _draft(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": "diagnose_slow_query",
        "slots": {},
        "missing": (),
        "confidence": 0.9,
        "source": IntentSource.MODEL,
    }
    return base | overrides


@pytest.mark.parametrize("field", _FORBIDDEN)
def test_execution_decision_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        IntentDraft(**_draft(**{field: "x"}))


def test_slots_reject_nested_structures() -> None:
    with pytest.raises(ValidationError):
        IntentDraft(**_draft(slots={"db": {"nested": "value"}}))


def test_slots_are_deeply_immutable() -> None:
    draft = IntentDraft(**_draft(slots={"db": "prod"}))
    with pytest.raises(TypeError):
        draft.slots["db"] = "other"  # type: ignore[index]


@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), float("inf")])
def test_confidence_outside_the_unit_interval_is_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        IntentDraft(**_draft(confidence=bad))


def _context(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "dev-local",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-2026-09-01",
    }
    return base | overrides


@pytest.mark.parametrize(
    "missing",
    ["tenant_id", "actor", "environment_id", "trace_id", "policy_revision"],
)
def test_request_context_requires_all_five_fields(missing: str) -> None:
    """环境无法解析出唯一值时 fail-closed，不取默认环境（ADR-007 D2）。"""
    payload = _context()
    del payload[missing]
    with pytest.raises(ValidationError):
        RequestContext(**payload)


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "ZZZ", "0" * 31, "0" * 33, "0" * 31 + "G", "0" * 31 + "A", "0" * 32 + "\n"],
)
def test_trace_id_must_be_32_lowercase_hex(bad: str) -> None:
    with pytest.raises(ValidationError):
        RequestContext(**_context(trace_id=bad))


def test_valid_context_is_accepted() -> None:
    ctx = RequestContext(**_context())
    assert ctx.trace_id == "0" * 32

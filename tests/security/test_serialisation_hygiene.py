"""契约的 dump / 往返必须无告警。

M4 要把契约持久化进 TaskStore 再读回；``MappingProxyType`` 不是 pydantic 认识的
序列化目标，缺少 ``PlainSerializer`` 时 ``model_dump()`` 会发
``PydanticSerializationUnexpectedValue``。这类告警在 ``-W error`` 下会直接失败，
更重要的是它意味着"dump 出来的东西可能不是你以为的形状"。

本文件用 ``warnings.catch_warnings(error=True)`` **只把告警局部提升为异常**，不改
全局 filter——`pytest-socket` 的阻断告警是预期行为，不应被牵连。
"""

import datetime as dt
import warnings

import pytest
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TOOL_CALL

from xiaowei_agent.contracts import (
    Contract,
    EvidenceEnvelope,
    ExternalSource,
    IntentDraft,
    IntentSource,
    PipelineStage,
    StageOutcome,
    TaskRecord,
    TaskStatus,
    TraceEvent,
)

pytestmark = pytest.mark.security

_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)


def _samples() -> list[Contract]:
    return [
        FIXTURE_PLAN,
        FIXTURE_PLAN.steps[0],
        FIXTURE_TOOL_CALL,
        IntentDraft(
            intent="i",
            slots={"db": "prod"},
            missing=(),
            confidence=0.9,
            source=IntentSource.MODEL,
        ),
        EvidenceEnvelope(
            evidence_id="e",
            capability_id="c",
            capability_version="1.0.0",
            facts=({"query_id": "q1"},),
            source="s",
            source_kind=ExternalSource.TOOL,
            captured_at=_AT,
            readonly=True,
            sampled=True,
            limitations=(),
        ),
        TraceEvent(
            event_id="e1",
            trace_id="0" * 32,
            task_id="t1",
            stage=PipelineStage.GATEWAY,
            outcome=StageOutcome.OK,
            occurred_at=_AT,
            capability_id=None,
            step_id="s1",
            policy_revision="r",
            error=None,
            detail={"k": "v"},
        ),
        TaskRecord(
            task_id="t1",
            tenant_id="dev-local",
            environment_id="dev",
            actor="alice",
            idempotency_key="idem-1",
            request_digest="c" * 64,
            status=TaskStatus.CREATED,
            version=0,
            created_seq=1,
            attempt_number=0,
            task_failure_count=0,
            next_attempt_at=None,
        ),
    ]


@pytest.mark.parametrize("model", _samples(), ids=lambda m: type(m).__name__)
def test_dump_emits_no_warnings(model: Contract) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        model.model_dump()
        model.model_dump_json()


@pytest.mark.parametrize("model", _samples(), ids=lambda m: type(m).__name__)
def test_json_round_trip_reconstructs_an_equal_object(model: Contract) -> None:
    """M4 持久化的前提：dump 出去再读回来必须相等。

    走 ``model_validate_json`` 而非 ``model_validate``：strict 模式下 JSON 路径才
    允许 ``str -> StrEnum`` / ISO -> ``datetime`` / ``list -> tuple``。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        payload = model.model_dump_json()
        assert type(model).model_validate_json(payload) == model

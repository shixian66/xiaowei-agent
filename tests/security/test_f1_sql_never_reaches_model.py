"""F1 SQL 消息只走规则来源交互事实；模型来源意图与嵌入 SQL 不执行（设计 §5.1、§5.3）。

F1 结果执行属 PR-E：在那之前 Runtime 在 Runner 之前以 ``read_class.not_allowed`` 拒绝
RESTRICTED 计划，所以这里的“目标唯一”路径断言到该拒绝为止，Gateway 调用恒为 0。
"""

import json
from typing import Any

import pytest
from tests.fakes.model import ScriptedModelAdapter
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.interaction_router import route_interaction
from xiaowei_agent.capabilities.readonly_query import ReadonlyQueryTargetCatalog
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    ClarificationReasonCode,
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionRejectionReasonCode,
    InteractionSource,
    ModelAdvisory,
    ModelUsage,
    PolicyReason,
    ResourcesConfig,
    RoutingDisposition,
    StarRocksResource,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.contracts.intent import READONLY_QUERY_INTENT, SLOW_QUERY_INTENT
from xiaowei_agent.persistence.store import SqlQuerySubmitCommand
from xiaowei_agent.rendering.generic import EMBEDDED_SQL_REJECTED

pytestmark = pytest.mark.security

_SQL = b"SELECT secret_marker_column FROM orders"
_MARKER = "secret_marker_column"


def _resource(resource_id: str, name: str) -> StarRocksResource:
    return StarRocksResource.model_validate(
        {
            "kind": "starrocks",
            "resource_id": resource_id,
            "environment": "dev",
            "display_name": name,
            "host": "fe.example.com",
            "port": 9030,
            "database": "app",
            "username": "f1_reader",
            "tls_mode": "verify_identity",
            "enabled": True,
            "f1_enabled": True,
        }
    )


def _catalog(*names: str) -> ReadonlyQueryTargetCatalog:
    resources = tuple(
        _resource(str(index) * 32 if index < 10 else "f" * 32, name)
        for index, name in enumerate(names, start=1)
    )
    return ReadonlyQueryTargetCatalog.from_resources(
        tenant_id="dev-local",
        config=ResourcesConfig(generation=1, resources=resources),
    )


def _model(draft: InteractionDraft | None = None) -> ScriptedModelAdapter:
    return ScriptedModelAdapter(
        interaction=InteractionModelResult(
            draft=draft
            or InteractionDraft(
                proposed_kind=InteractionKind.CONVERSATION,
                confidence=0.9,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(),
        ),
        advisory=AdvisoryModelResult(
            advisory=ModelAdvisory(analysis="不应被调用", suggestions=(), uncertainties=()),
            usage=ModelUsage(),
        ),
    )


def _harness(model: ScriptedModelAdapter, *names: str) -> RuntimeHarness:
    return RuntimeHarness(
        GOLDEN,
        interaction_classifier=model,
        readonly_query_targets=_catalog(*names),
    )


def _lookup(harness: RuntimeHarness, task_id: str) -> TaskLookup:
    return TaskLookup(
        task_id=task_id,
        tenant_id=harness.context.tenant_id,
        environment_id=harness.context.environment_id,
    )


async def _execute(harness: RuntimeHarness, task_id: str) -> Any:
    attempt = await harness.store.begin_task_attempt(
        command=harness.attempt_command(task_id)
    )
    submission = await harness.store.get_submission(lookup=_lookup(harness, task_id))
    return await harness.runtime.execute_task(grant=attempt.grant, submission=submission)


async def _submit_sql(harness: RuntimeHarness, *, key: str = "sql-1") -> str:
    result = await harness.store.submit_sql_query(
        command=SqlQuerySubmitCommand(
            context=harness.context,
            sql_bytes=_SQL,
            idempotency_key=key,
            as_of=harness.as_of,
        )
    )
    assert result.task is not None
    return result.task.task_id


def _persisted_text(harness: RuntimeHarness) -> str:
    """交互事实、澄清记录、提交事实、trace 与审计：都不得含 SQL 原文。"""
    state = harness.state
    parts: list[object] = [
        [artifact.model_dump(mode="json") for artifact in state.interaction_artifacts.values()],
        [record.model_dump(mode="json") for record in state.clarification_records.values()],
        [submission.model_dump(mode="json") for submission in state.submissions.values()],
        [event.model_dump(mode="json") for event in harness.sink.events],
        [
            event.model_dump(mode="json")
            for events in state.audit_events.values()
            for event in events
        ],
    ]
    return json.dumps(parts, ensure_ascii=False, default=str)


async def test_f1_sql_never_reaches_model() -> None:
    model = _model()
    harness = _harness(model, "Orders")
    task_id = await _submit_sql(harness)

    outcome = await _execute(harness, task_id)

    assert model.interaction_requests == []
    artifact = harness.state.interaction_artifacts[task_id]
    assert artifact.origin == "rule"
    assert artifact.draft.source is InteractionSource.RULE
    assert artifact.draft.capability_draft is not None
    assert artifact.draft.capability_draft.intent == READONLY_QUERY_INTENT
    assert artifact.draft.capability_draft.slots == {}
    # 目标唯一：计划可编译，但 RESTRICTED 读在 PR-E 接入前于 Runner 之前被拒。
    assert outcome.status is TaskStatus.REJECTED
    assert outcome.terminal_reason == PolicyReason.READ_CLASS_NOT_ALLOWED.value
    assert harness.calls == []
    assert _MARKER not in _persisted_text(harness)


async def test_zero_targets_reject_before_any_gateway_call() -> None:
    model = _model()
    harness = _harness(model)
    outcome = await _execute(harness, await _submit_sql(harness))
    assert outcome.status is TaskStatus.REJECTED
    assert model.interaction_requests == []
    assert harness.calls == []


async def test_several_targets_ask_and_the_exact_answer_is_rule_interpreted() -> None:
    model = _model()
    harness = _harness(model, "Orders", "指标库")
    parent_id = await _submit_sql(harness)

    asked = await _execute(harness, parent_id)

    assert asked.status is TaskStatus.CLARIFICATION_REQUIRED
    record = harness.state.clarification_records[parent_id]
    assert record.reason_code is ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED
    selection = record.subject.target_selection
    assert [option.display_name for option in selection.options] == ["Orders", "指标库"]
    view = await harness.runtime.query_task(lookup=_lookup(harness, parent_id))
    assert view.clarification is not None
    assert "Orders" in view.clarification.prompt

    for index, (answer, expected) in enumerate(
        (("orders", TaskStatus.CLARIFICATION_REQUIRED), ("Orders", TaskStatus.REJECTED))
    ):
        child = await harness.store.create_clarification_child(
            submission=harness.submission(answer, idempotency_key=f"answer-{index}").model_copy(
                update={"clarification_parent_task_id": parent_id}
            ),
            authenticated_channel_owner=harness.context.actor,
        )
        outcome = await _execute(harness, child.task_id)
        assert outcome.status is expected
        parent_id = child.task_id

    # 精确回答后到达 RESTRICTED 读拒绝（PR-E 前），全程没有模型调用、没有 Gateway 调用。
    assert outcome.terminal_reason == PolicyReason.READ_CLASS_NOT_ALLOWED.value
    assert model.interaction_requests == []
    assert harness.calls == []
    assert _MARKER not in _persisted_text(harness)


@pytest.mark.parametrize("source", [InteractionSource.MODEL, InteractionSource.FAKE])
def test_f1_model_origin_query_intent_never_executes(source: InteractionSource) -> None:
    harness = _harness(_model(), "Orders")
    draft = InteractionDraft(
        proposed_kind=InteractionKind.CAPABILITY_REQUEST,
        capability_draft=IntentDraft(
            intent=READONLY_QUERY_INTENT,
            slots={},
            missing=(),
            confidence=0.99,
            source=IntentSource.MODEL,
        ),
        confidence=0.99,
        source=source,
    )
    decision = route_interaction(draft=draft, context=harness.context)
    assert decision.disposition is RoutingDisposition.REFUSE
    assert decision.reason_code is InteractionRejectionReasonCode.CAPABILITY_DRAFT_FORBIDDEN
    assert decision.intent_draft is None


async def test_f1_model_cannot_select_the_query_intent_for_mixed_text() -> None:
    # 供应商响应里的同名意图在接受前就被闭集拒绝，退回规则解释；不建 SqlArtifact。
    model = _model(
        InteractionDraft(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            capability_draft=IntentDraft(
                intent=READONLY_QUERY_INTENT,
                slots={},
                missing=(),
                confidence=0.99,
                source=IntentSource.MODEL,
            ),
            confidence=0.99,
            source=InteractionSource.MODEL,
        )
    )
    harness = _harness(model, "Orders")
    await harness.handle("帮我执行一下订单查询")
    record = await harness.store.get(lookup=_lookup(harness, harness.task_id))
    assert record.status is not TaskStatus.SUCCEEDED
    assert harness.state.sql_artifacts == {}
    assert harness.calls == []


@pytest.mark.parametrize("with_model", [False, True])
async def test_f1_embedded_sql_is_not_executed(with_model: bool) -> None:
    # 规则会把“慢SQL”识别为慢查询诊断；夹带 SQL 时不论草案来源都拒绝。
    model = _model(
        InteractionDraft(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            capability_draft=IntentDraft(
                intent=SLOW_QUERY_INTENT,
                slots={},
                missing=(),
                confidence=0.9,
                source=IntentSource.MODEL,
            ),
            confidence=0.9,
            source=InteractionSource.MODEL,
        )
    )
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model if with_model else None)

    payload = await harness.handle("帮我分析这条慢SQL：SELECT a FROM t WHERE b = 1")

    record = await harness.store.get(lookup=_lookup(harness, harness.task_id))
    assert record.status is TaskStatus.REJECTED
    assert record.terminal_reason == (
        InteractionRejectionReasonCode.EMBEDDED_SQL_NOT_EXECUTED.value
    )
    assert payload.answer == EMBEDDED_SQL_REJECTED
    assert harness.calls == []
    assert harness.state.sql_artifacts == {}


@pytest.mark.parametrize(
    "text",
    [
        "最近30分钟有哪些慢查询",
        "最近30分钟有哪些慢查询\n```python\nprint('select x')\n```",
        "select 一下最近30分钟的慢查询",
    ],
)
async def test_f1_non_sql_code_block_is_not_blocked(text: str) -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle(text)
    record = await harness.store.get(lookup=_lookup(harness, harness.task_id))
    assert record.terminal_reason != (
        InteractionRejectionReasonCode.EMBEDDED_SQL_NOT_EXECUTED.value
    )
    assert record.status is TaskStatus.SUCCEEDED
    assert len(harness.calls) >= 1

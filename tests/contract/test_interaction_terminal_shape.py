"""每条入口终态的两个不变量：拒绝码可归因、终态恰好一条 REFLECTION。

这两条以前都只是"碰巧成立"：四种 pre-plan 拒绝在任务记录上全是 `terminal_reason=None`，
而普通对话是全系统唯一不发 REFLECTION 的终态。两者都不影响用户拿到的结果，但都让
"这条任务为什么这样结束"在**任务事实**上无法回答，只能回去翻 trace 或读代码。
"""

from typing import Any

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import (
    AttemptIntent,
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionRejectionReasonCode,
    InteractionSource,
    ModelUsage,
    PipelineStage,
    StageOutcome,
    TaskStatus,
)
from xiaowei_agent.observability.sink import Delivery
from xiaowei_agent.persistence.store import TaskAttemptCommand


class _Port:
    def __init__(self, draft: InteractionDraft) -> None:
        self.draft = draft
        self.calls = 0

    async def classify(self, request: Any) -> InteractionModelResult:
        self.calls += 1
        return InteractionModelResult(draft=self.draft, usage=ModelUsage())


def _capability_draft(**slots: str) -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"window_minutes": "30"} | slots,
        missing=(),
        confidence=0.8,
        source=IntentSource.MODEL,
    )


def _draft(
    kind: InteractionKind, capability: IntentDraft | None = None
) -> InteractionDraft:
    return InteractionDraft(
        proposed_kind=kind,
        capability_draft=capability,
        confidence=0.8,
        source=InteractionSource.MODEL,
    )


async def _run(harness: RuntimeHarness, text: str) -> Any:
    submission = harness.submission(text)
    record = await harness.runtime.submit_task(submission=submission)
    harness.task_id = record.task_id
    attempt = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=record.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-shape",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempt.grant is not None
    return await harness.runtime.execute_task(
        grant=attempt.grant, submission=submission
    )


_ROUTER_REJECTIONS = (
    (
        _draft(InteractionKind.KNOWLEDGE_LOOKUP),
        InteractionRejectionReasonCode.ROUTE_NOT_AVAILABLE,
    ),
    (
        _draft(InteractionKind.LOG_ANALYSIS),
        InteractionRejectionReasonCode.ROUTE_NOT_AVAILABLE,
    ),
    (
        _draft(InteractionKind.CONVERSATION, _capability_draft()),
        InteractionRejectionReasonCode.CAPABILITY_DRAFT_FORBIDDEN,
    ),
    (
        _draft(InteractionKind.CAPABILITY_REQUEST),
        InteractionRejectionReasonCode.CAPABILITY_DRAFT_MISSING,
    ),
    (
        _draft(
            InteractionKind.CAPABILITY_REQUEST,
            _capability_draft(environment_id="prod"),
        ),
        InteractionRejectionReasonCode.ENVIRONMENT_CONTEXT_MISMATCH,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft", "expected"),
    _ROUTER_REJECTIONS,
    ids=[reason.value for _, reason in _ROUTER_REJECTIONS],
)
async def test_router_rejection_carries_its_closed_set_reason(
    draft: InteractionDraft, expected: InteractionRejectionReasonCode
) -> None:
    """Router 已经算出的拒绝码必须落到任务记录上。

    全写 `None` 时这五条在任务事实上完全无法区分——"路由不支持"和"模型夹带了
    capability draft"是两个成因、两种处置，却长得一模一样。
    """
    harness = RuntimeHarness(GOLDEN, interaction_classifier=_Port(draft))

    outcome = await _run(harness, "随便一句")

    assert outcome.status is TaskStatus.REJECTED
    assert outcome.terminal_reason == expected.value
    record = await harness.store.get(lookup=harness.lookup)
    assert record.terminal_reason == expected.value
    assert harness.calls == []


@pytest.mark.asyncio
async def test_clarification_terminal_keeps_its_reason_in_the_record_not_the_task() -> (
    None
):
    """澄清的原因属于 ClarificationRecord，不得混进 `terminal_reason`。

    两个域共用一个字段，读任务记录的人就无法只凭 `terminal_reason` 判断这是一次
    拒绝还是一次澄清。
    """
    harness = RuntimeHarness(
        GOLDEN, interaction_classifier=_Port(_draft(InteractionKind.UNKNOWN))
    )

    outcome = await _run(harness, "随便一句")

    assert outcome.status is TaskStatus.CLARIFICATION_REQUIRED
    assert outcome.terminal_reason is None
    stored = await harness.clarification_records.load(task_id=harness.task_id)
    assert stored is not None
    assert stored.reason_code is not None


_TERMINAL_ROUTES = (
    ("conversation", _draft(InteractionKind.CONVERSATION)),
    ("route_refused", _draft(InteractionKind.KNOWLEDGE_LOOKUP)),
    ("clarify", _draft(InteractionKind.UNKNOWN)),
    (
        "capability",
        _draft(InteractionKind.CAPABILITY_REQUEST, _capability_draft()),
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "draft"), _TERMINAL_ROUTES, ids=[label for label, _ in _TERMINAL_ROUTES]
)
async def test_every_terminal_task_emits_exactly_one_reflection(
    label: str, draft: InteractionDraft
) -> None:
    """终态任务恰好一条 REFLECTION——普通对话也不例外。

    普通对话绕过 `_finalize` 是对的（没有证据可评，`assess_evidence` 会把它降级成
    REJECTED），但绕过不能连审计形状一起绕掉：少这条事件，"这个任务有没有被评过
    可答性"在 trace 上就没有答案，而它是全链唯一一条终态不变量。
    """
    del label
    harness = RuntimeHarness(GOLDEN, interaction_classifier=_Port(draft))

    outcome = await _run(harness, "最近30分钟有哪些慢查询")

    reflections = [
        event
        for event in harness.sink.events
        if event.stage is PipelineStage.REFLECTION
    ]
    assert len(reflections) == 1
    assert reflections[0].task_id == harness.task_id
    # REFLECTION 在终态 LIFECYCLE 之前：与 `_finalize` 同序，否则读 trace 的人会
    # 以为可答性是在任务已经结束之后才评的。
    stages = [event.stage for event in harness.sink.events]
    assert stages.index(PipelineStage.REFLECTION) < len(stages) - 1
    assert stages[stages.index(PipelineStage.REFLECTION) + 1] is (
        PipelineStage.LIFECYCLE
    )
    assert outcome.status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.REJECTED,
        TaskStatus.CLARIFICATION_REQUIRED,
    }


@pytest.mark.asyncio
async def test_conversation_reflection_is_committed_with_the_terminal_transition() -> (
    None
):
    """REFLECTION 与终态迁移同一条命令提交，不是事后补发。

    分开写就会出现"状态已终态、审计没落"或者反过来；这条任务没有 plan、没有
    evidence，审计事件是它仅有的过程事实。
    """
    harness = RuntimeHarness(
        GOLDEN, interaction_classifier=_Port(_draft(InteractionKind.CONVERSATION))
    )

    await _run(harness, "你能做什么？")

    index = next(
        position
        for position, event in enumerate(harness.sink.events)
        if event.stage is PipelineStage.REFLECTION
    )
    assert harness.sink.events[index].outcome is StageOutcome.OK
    # COMMAND_COMMITTED 只有在事件随迁移命令一起提交成功时才会发出；事后单独
    # 补发的事件拿不到这个 delivery。
    assert harness.sink.deliveries[index] is Delivery.COMMAND_COMMITTED
    assert harness.sink.deliveries[index + 1] is Delivery.COMMAND_COMMITTED

"""显式父链在 Worker 模型调用前再次执行完整安全校验。"""

from typing import Any

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness
from tests.unit.test_context_assembler import _bound_task, _Projector

from xiaowei_agent.application.context import (
    ContextAssembler,
    ParentContextRejectedError,
    ParentContextUnavailableError,
)
from xiaowei_agent.application.runtime import RetryableTaskError
from xiaowei_agent.contracts import (
    AttemptIntent,
    Channel,
    ChannelKind,
    RenderPayload,
    TaskStatus,
)
from xiaowei_agent.persistence.fake import InMemoryChannelStore
from xiaowei_agent.persistence.store import TaskAttemptCommand, submission_digest

pytestmark = pytest.mark.security


@pytest.fixture
def channels(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


@pytest.mark.parametrize(
    "case",
    ("nonterminal", "actor", "owner", "channel", "tenant"),
)
async def test_worker_rejects_every_parent_authority_and_text_boundary_drift(
    case, store, channels, context, clock
) -> None:
    parent_context = context
    terminal = True
    owner = "subject-alice"
    channel = ChannelKind.WEB
    text = "safe parent"
    if case == "nonterminal":
        terminal = False
    elif case == "actor":
        parent_context = context.model_copy(update={"actor": "mallory"})
    elif case == "owner":
        owner = "subject-mallory"
    elif case == "channel":
        channel = ChannelKind.FEISHU_PRIVATE
    elif case == "tenant":
        parent_context = context.model_copy(update={"tenant_id": "other-tenant"})
    parent, _ = await _bound_task(
        store=store,
        channels=channels,
        context=parent_context,
        clock=clock,
        suffix=f"security-{case}-parent",
        text=text,
        owner=owner,
        channel=channel,
        terminal=terminal,
    )
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix=f"security-{case}-child",
        parent_task_id=parent.task_id,
    )
    assembler = ContextAssembler(
        task_store=store,
        channel_store=channels,
        task_projector=_Projector(),
    )

    with pytest.raises(ParentContextRejectedError):
        await assembler.assemble(task_id=child.task_id, submission=child_submission)


async def test_worker_rejects_invalid_utf8_in_safe_projection(
    store, channels, context, clock
) -> None:
    parent, _ = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="invalid-utf8-parent",
        terminal=True,
    )
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="invalid-utf8-child",
        parent_task_id=parent.task_id,
    )

    class InvalidProjector:
        async def project_recorded(self, *, record: Any) -> RenderPayload:
            return RenderPayload(
                answer="invalid-\ud800",
                sections=(),
                next_steps=(),
                status=record.status,
                refs=(),
            )

    assembler = ContextAssembler(
        task_store=store,
        channel_store=channels,
        task_projector=InvalidProjector(),
    )

    with pytest.raises(ParentContextRejectedError):
        await assembler.assemble(task_id=child.task_id, submission=child_submission)


async def test_worker_rejects_oversized_text_in_safe_projection(
    store, channels, context, clock
) -> None:
    parent, _ = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="oversized-projection-parent",
        terminal=True,
    )
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="oversized-projection-child",
        parent_task_id=parent.task_id,
    )

    class OversizedProjector:
        async def project_recorded(self, *, record: Any) -> RenderPayload:
            return RenderPayload(
                answer="x" * 8_193,
                sections=(),
                next_steps=(),
                status=record.status,
                refs=(),
            )

    assembler = ContextAssembler(
        task_store=store,
        channel_store=channels,
        task_projector=OversizedProjector(),
    )

    with pytest.raises(ParentContextRejectedError):
        await assembler.assemble(task_id=child.task_id, submission=child_submission)


class _NeverCalledModel:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_intent(self, request: Any) -> Any:
        del request
        self.calls += 1
        raise AssertionError("invalid parent must stop before the model")


class _FailingAssembler:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def assemble(self, *, task_id: str, submission: Any) -> Any:
        del task_id, submission
        self.calls += 1
        raise self.error


async def _parented_attempt(harness: RuntimeHarness) -> tuple[Any, Any]:
    base = harness.submission("最近30分钟有哪些慢查询")
    submission = base.model_copy(
        update={
            "envelope": base.envelope.model_copy(update={"channel": Channel.WEB}),
            "parent_task_id": "explicit-parent",
        }
    )
    view = await harness.runtime.submit_task(submission=submission)
    attempted = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-parent-security",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempted.grant is not None and attempted.submission is not None
    return attempted.grant, attempted.submission


async def test_rejected_parent_makes_zero_model_or_gateway_calls() -> None:
    model = _NeverCalledModel()
    assembler = _FailingAssembler(ParentContextRejectedError())
    harness = RuntimeHarness(
        GOLDEN,
        intent_model=model,
        context_assembler=assembler,
    )
    grant, submission = await _parented_attempt(harness)

    outcome = await harness.runtime.execute_task(grant=grant, submission=submission)

    assert outcome.status is TaskStatus.REJECTED
    assert assembler.calls == 1
    assert model.calls == 0
    assert harness.calls == []


async def test_unbound_child_is_retryable_without_a_model_fallback() -> None:
    model = _NeverCalledModel()
    assembler = _FailingAssembler(ParentContextUnavailableError())
    harness = RuntimeHarness(
        GOLDEN,
        intent_model=model,
        context_assembler=assembler,
    )
    grant, submission = await _parented_attempt(harness)

    with pytest.raises(RetryableTaskError):
        await harness.runtime.execute_task(grant=grant, submission=submission)

    assert assembler.calls == 1
    assert model.calls == 0
    assert harness.calls == []


async def test_worker_rejects_a_parented_non_web_child(
    store, channels, context, clock
) -> None:
    parent, _ = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="web-parent-for-non-web-child",
        terminal=True,
    )
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="non-web-child",
        parent_task_id=parent.task_id,
        channel=ChannelKind.FEISHU_PRIVATE,
    )
    assembler = ContextAssembler(
        task_store=store,
        channel_store=channels,
        task_projector=_Projector(),
    )

    with pytest.raises(ParentContextRejectedError):
        await assembler.assemble(task_id=child.task_id, submission=child_submission)


async def test_worker_rejects_a_corrupt_parent_cycle(
    store, channels, context, clock, memory_state
) -> None:
    parent, parent_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="worker-cycle-parent",
        terminal=True,
    )
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="worker-cycle-child",
        parent_task_id=parent.task_id,
    )
    corrupt_parent = parent_submission.model_copy(
        update={"parent_task_id": parent.task_id}
    )
    memory_state.submissions[parent.task_id] = corrupt_parent
    memory_state.submission_digests[parent.task_id] = submission_digest(corrupt_parent)
    assembler = ContextAssembler(
        task_store=store,
        channel_store=channels,
        task_projector=_Projector(),
    )

    with pytest.raises(ParentContextRejectedError):
        await assembler.assemble(task_id=child.task_id, submission=child_submission)

"""显式父链只组装安全、同身份、完整且有界的历史轮次。"""

import json
from typing import Any

import pytest
from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

from xiaowei_agent.application.context import (
    ContextAssembler,
    ParentContextRejectedError,
    ParentContextUnavailableError,
)
from xiaowei_agent.contracts import (
    MAX_MODEL_HISTORY_CHARACTERS,
    Channel,
    ChannelKind,
    RenderPayload,
    RenderSection,
    TaskStatus,
)
from xiaowei_agent.persistence.channel import BindTaskCommand
from xiaowei_agent.persistence.fake import InMemoryChannelStore


class _Projector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def project_recorded(self, *, record: Any) -> RenderPayload:
        self.calls.append(record.task_id)
        return RenderPayload(
            answer=f"safe answer {record.task_id}",
            sections=(
                RenderSection(
                    title="diagnosis",
                    body=f"safe body {record.task_id}",
                    refs=(f"evidence:{record.task_id}",),
                ),
            ),
            next_steps=("keep observing",),
            status=record.status,
            refs=(f"root:{record.task_id}",),
        )


async def _bound_task(
    *,
    store: Any,
    channels: InMemoryChannelStore,
    context: Any,
    clock: Any,
    suffix: str,
    parent_task_id: str | None = None,
    text: str | None = None,
    owner: str = "subject-alice",
    channel: ChannelKind = ChannelKind.WEB,
    terminal: bool = False,
) -> tuple[Any, Any]:
    submission = make_submission(
        context,
        envelope=make_envelope(
            request_id=f"request-{suffix}",
            idempotency_key=f"context-{suffix}",
            channel=Channel.WEB if channel is ChannelKind.WEB else Channel.FEISHU,
            text=text or f"user text {suffix}",
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            actor=context.actor,
        ),
        parent_task_id=parent_task_id,
    )
    record = await store.create_task(submission=submission)
    await channels.bind_task(
        command=BindTaskCommand(
            task_id=record.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            channel=channel,
            initiator_subject_ref=owner,
            conversation_ref="chat-1" if channel is ChannelKind.FEISHU_GROUP else None,
            source_event_ref=f"event-{suffix}",
            created_at=clock(),
        )
    )
    if terminal:
        await drive_to_terminal(store, lookup_for(record), TaskStatus.SUCCEEDED)
    return record, submission


@pytest.fixture
def channels(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


@pytest.fixture
def projector():
    return _Projector()


@pytest.fixture
def assembler(store, channels, projector):
    return ContextAssembler(
        task_store=store,
        channel_store=channels,
        task_projector=projector,
    )


async def test_no_parent_returns_empty_context_without_reading_a_binding(
    assembler, store, context
) -> None:
    submission = make_submission(context)

    assembled = await assembler.assemble(task_id="not-persisted", submission=submission)

    assert assembled.history == ()
    assert assembled.truncated is False


async def test_parent_chain_is_chronological_scrubbed_and_excludes_refs(
    assembler, store, channels, projector, context, clock
) -> None:
    fake_secret = "token=" + "context-fixture-value"
    root, _ = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="root",
        text=f"inspect {fake_secret}",
        terminal=True,
    )
    parent, _ = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="parent",
        parent_task_id=root.task_id,
        terminal=True,
    )
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="child",
        parent_task_id=parent.task_id,
    )

    assembled = await assembler.assemble(
        task_id=child.task_id,
        submission=child_submission,
    )

    rounds = [json.loads(item) for item in assembled.history]
    assert len(rounds) == 2
    assert rounds[0]["user"].startswith("inspect ")
    assert rounds[1]["user"] == "user text parent"
    assert rounds[0]["user"] != f"inspect {fake_secret}"
    assert "***" in rounds[0]["user"]
    assert all("refs" not in item["assistant"] for item in rounds)
    assert fake_secret not in "".join(assembled.history)
    assert projector.calls == [parent.task_id, root.task_id]
    assert assembled.truncated is False


async def test_context_keeps_only_twenty_newest_complete_parent_rounds(
    assembler, store, channels, context, clock
) -> None:
    parent_task_id = None
    for index in range(21):
        record, _ = await _bound_task(
            store=store,
            channels=channels,
            context=context,
            clock=clock,
            suffix=f"limit-{index}",
            parent_task_id=parent_task_id,
            terminal=True,
        )
        parent_task_id = record.task_id
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="limit-child",
        parent_task_id=parent_task_id,
    )

    assembled = await assembler.assemble(
        task_id=child.task_id,
        submission=child_submission,
    )

    retained_users = [json.loads(item)["user"] for item in assembled.history]
    assert retained_users == [f"user text limit-{index}" for index in range(1, 21)]
    assert assembled.truncated is True


async def test_history_capacity_drops_old_rounds_whole(
    assembler, store, channels, context, clock
) -> None:
    parent_task_id = None
    for index in range(10):
        record, _ = await _bound_task(
            store=store,
            channels=channels,
            context=context,
            clock=clock,
            suffix=f"bytes-{index}",
            parent_task_id=parent_task_id,
            text=f"round {index} " + ("x" * 7_000),
            terminal=True,
        )
        parent_task_id = record.task_id
    child, child_submission = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="bytes-child",
        parent_task_id=parent_task_id,
    )

    assembled = await assembler.assemble(
        task_id=child.task_id,
        submission=child_submission,
    )

    retained_users = [json.loads(item)["user"] for item in assembled.history]
    retained_indices = [int(item.split(" ", 2)[1]) for item in retained_users]
    assert retained_indices == list(range(10 - len(retained_indices), 10))
    assert sum(map(len, assembled.history)) <= MAX_MODEL_HISTORY_CHARACTERS
    assert assembled.truncated is True


async def test_missing_child_binding_retries_but_missing_parent_binding_rejects(
    assembler, store, channels, context, clock
) -> None:
    parent, _ = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="bound-parent",
        terminal=True,
    )
    child_submission = make_submission(
        context,
        envelope=make_envelope(
            request_id="request-unbound-child",
            idempotency_key="context-unbound-child",
            channel=Channel.WEB,
        ),
        parent_task_id=parent.task_id,
    )
    unbound_child = await store.create_task(submission=child_submission)

    with pytest.raises(ParentContextUnavailableError):
        await assembler.assemble(
            task_id=unbound_child.task_id,
            submission=child_submission,
        )

    child, persisted_child = await _bound_task(
        store=store,
        channels=channels,
        context=context,
        clock=clock,
        suffix="bound-child",
        parent_task_id="unknown-parent",
    )
    with pytest.raises(ParentContextRejectedError):
        await assembler.assemble(task_id=child.task_id, submission=persisted_child)

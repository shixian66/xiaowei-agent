"""投影发送前版本稳定、tenant 并发和提交 fencing 的安全反证。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

from xiaowei_agent.application.channel_projection import (
    ChannelProjectionService,
    TaskProjectionPort,
)
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ChannelKind,
    DestinationKind,
    ProjectionState,
    RenderPayload,
    TaskRecord,
    TaskStatus,
    TaskView,
    task_query_path,
)
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    CreateProjectionSubscriptionCommand,
    ProjectionSubscription,
)
from xiaowei_agent.persistence.fake import InMemoryChannelStore, InMemoryTaskStore
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import TransitionCommand
from xiaowei_agent.rendering.feishu import RenderedFeishuCard

pytestmark = pytest.mark.security


@dataclass(frozen=True)
class _Settings:
    tenant_id: str = "dev-local"
    environment_id: str = "dev"
    web_public_origin: str = "https://ops.example.test"
    projection_claim_ttl_seconds: int = 15
    projection_batch_limit: int = 10
    projection_provider_max_attempts: int = 3
    projection_provider_backoff_base_seconds: float = 1.0
    projection_retry_after_cap_seconds: float = 30.0
    projection_tenant_concurrency: int = 2
    projection_task_poll_base_seconds: float = 2.0
    projection_task_poll_cap_seconds: float = 10.0
    channel_worker_poll_interval_seconds: float = 1.0


def _view(record: TaskRecord) -> TaskView:
    render = None
    if record.status in TERMINAL_STATUSES:
        render = RenderPayload(
            answer="安全结论",
            sections=(),
            next_steps=(),
            status=record.status,
            refs=("evidence:safe",),
        )
    return TaskView(
        task_id=record.task_id,
        status=record.status,
        render=render,
        query_path=task_query_path(record.task_id),
    )


class _Runtime:
    async def project_task(self, *, record: TaskRecord) -> TaskView:
        return _view(record)


class _Messages:
    def __init__(
        self,
        *,
        message_ref: str,
        before_return: Callable[[], Awaitable[None]] | None = None,
        block: bool = False,
    ) -> None:
        self.message_ref = message_ref
        self.before_return = before_return
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.entered = asyncio.Event()
        self.two_active = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def _result(self) -> str:
        self.calls += 1
        self.active += 1
        self.entered.set()
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active >= 2:
            self.two_active.set()
        try:
            await self.release.wait()
            if self.before_return is not None:
                await self.before_return()
            return self.message_ref
        finally:
            self.active -= 1

    async def send_to_chat(
        self,
        *,
        conversation_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        del conversation_ref, card, idempotency_ref
        return await self._result()

    async def send_to_user(
        self,
        *,
        subject_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        del subject_ref, card, idempotency_ref
        return await self._result()

    async def update_card(
        self, *, message_ref: str, card: RenderedFeishuCard
    ) -> None:
        del message_ref, card
        await self._result()


def _stores(clock) -> tuple[InMemoryTaskStore, InMemoryChannelStore, InMemoryPersistenceState]:
    state = InMemoryPersistenceState()
    return (
        InMemoryTaskStore(clock=clock, state=state),
        InMemoryChannelStore(clock=clock, state=state),
        state,
    )


def _service(
    *,
    tasks: InMemoryTaskStore,
    channels: InMemoryChannelStore,
    messages: _Messages,
    clock,
    owner: str,
    runtime: TaskProjectionPort | None = None,
) -> ChannelProjectionService:
    return ChannelProjectionService(
        runtime=runtime or _Runtime(),
        task_store=tasks,
        channel_store=channels,
        message_port=messages,
        clock=clock,
        settings=_Settings(),
        sleep=asyncio.sleep,
        owner=owner,
    )


async def _bind_task(
    *,
    tasks: InMemoryTaskStore,
    channels: InMemoryChannelStore,
    state: InMemoryPersistenceState,
    clock,
    context,
    suffix: str,
) -> tuple[TaskRecord, ProjectionSubscription]:
    task = await tasks.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id=f"request-{suffix}",
                idempotency_key=f"projection-{suffix}",
                text="检查最近三十分钟慢查询",
            ),
            as_of=clock(),
        )
    )
    await channels.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref=f"chat-{suffix}",
            source_event_ref=f"event-{suffix}",
            created_at=clock(),
            projection=CreateProjectionSubscriptionCommand(
                task_id=task.task_id,
                destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
                destination_ref=f"chat-{suffix}",
                initial_state=ProjectionState.PENDING_INITIAL,
                next_attempt_at=clock(),
            ),
        )
    )
    subscription = next(
        item
        for item in state.projection_subscriptions.values()
        if item.task_id == task.task_id
    )
    return task, subscription


@pytest.mark.asyncio
async def test_stale_sender_cannot_overwrite_a_reclaimed_subscription(
    clock, context, caplog: pytest.LogCaptureFixture
) -> None:
    tasks, channels, state = _stores(clock)
    task, subscription = await _bind_task(
        tasks=tasks,
        channels=channels,
        state=state,
        clock=clock,
        context=context,
        suffix="fencing",
    )
    await drive_to_terminal(tasks, lookup_for(task), TaskStatus.SUCCEEDED)
    winner_messages = _Messages(message_ref="winner-message")
    winner = _service(
        tasks=tasks,
        channels=channels,
        messages=winner_messages,
        clock=clock,
        owner="worker-winner",
    )
    winner_messages.release.clear()
    winner_poll: asyncio.Task[int] | None = None

    async def expire_and_reclaim() -> None:
        nonlocal winner_poll
        clock.advance(seconds=15)
        winner_poll = asyncio.create_task(winner.poll_once())
        await asyncio.wait_for(winner_messages.entered.wait(), timeout=1)

    stale_messages = _Messages(
        message_ref="stale-message",
        before_return=expire_and_reclaim,
    )
    stale = _service(
        tasks=tasks,
        channels=channels,
        messages=stale_messages,
        clock=clock,
        owner="worker-stale",
    )

    with caplog.at_level("WARNING"):
        assert await stale.poll_once() == 0

    assert [
        record.failure_kind
        for record in caplog.records
        if getattr(record, "failure_kind", None) == "claim_lost"
    ] == ["claim_lost"]

    while_winner_is_live = state.projection_subscriptions[
        subscription.subscription_id
    ]
    assert while_winner_is_live.state is ProjectionState.PENDING_INITIAL
    assert while_winner_is_live.claim_owner == "worker-winner"
    assert while_winner_is_live.source_message_ref is None

    winner_messages.release.set()
    assert winner_poll is not None
    assert await winner_poll == 1

    persisted = state.projection_subscriptions[subscription.subscription_id]
    assert persisted.state is ProjectionState.COMPLETED
    assert persisted.source_message_ref == "winner-message"
    assert persisted.fencing_token is None
    assert stale_messages.calls == 1
    assert winner_messages.calls == 1


@pytest.mark.asyncio
async def test_continuous_task_version_change_never_sends_a_mixed_card(
    clock, context
) -> None:
    tasks, channels, state = _stores(clock)
    task, subscription = await _bind_task(
        tasks=tasks,
        channels=channels,
        state=state,
        clock=clock,
        context=context,
        suffix="moving-version",
    )

    class AdvancingRuntime:
        async def project_task(self, *, record: TaskRecord) -> TaskView:
            view = _view(record)
            following = {
                TaskStatus.CREATED: TaskStatus.PLANNING,
                TaskStatus.PLANNING: TaskStatus.RUNNING,
                TaskStatus.RUNNING: TaskStatus.SUCCEEDED,
            }.get(record.status)
            if following is not None:
                result = await tasks.transition(
                    command=TransitionCommand(
                        task_id=record.task_id,
                        expected_version=record.version,
                        to_status=following,
                    )
                )
                assert result.applied
            return view

    messages = _Messages(message_ref="must-not-send")
    service = _service(
        tasks=tasks,
        channels=channels,
        messages=messages,
        clock=clock,
        owner="worker-version",
        runtime=AdvancingRuntime(),
    )

    assert await service.poll_once() == 1

    persisted = state.projection_subscriptions[subscription.subscription_id]
    assert persisted.state is ProjectionState.PENDING_INITIAL
    assert persisted.claim_owner == "worker-version"
    assert messages.calls == 0
    assert (await tasks.get(lookup=lookup_for(task))).status is TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_tenant_outbound_concurrency_never_exceeds_two(
    clock, context
) -> None:
    tasks, channels, state = _stores(clock)
    for index in range(5):
        await _bind_task(
            tasks=tasks,
            channels=channels,
            state=state,
            clock=clock,
            context=context,
            suffix=f"concurrency-{index}",
        )
    messages = _Messages(message_ref="message", block=True)
    service = _service(
        tasks=tasks,
        channels=channels,
        messages=messages,
        clock=clock,
        owner="worker-concurrency",
    )

    polling = asyncio.create_task(service.poll_once())
    await asyncio.wait_for(messages.two_active.wait(), timeout=1)
    assert messages.maximum_active == 2
    messages.release.set()

    assert await polling == 5
    assert messages.calls == 5
    assert messages.maximum_active == 2

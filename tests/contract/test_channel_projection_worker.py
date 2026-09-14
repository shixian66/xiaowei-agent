"""M7 projection worker 对任务真源、投递状态和 provider 失败的契约。"""

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

from xiaowei_agent.application.channel_projection import (
    ChannelMessageError,
    ChannelProjectionInvariantError,
    ChannelProjectionService,
)
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ChannelKind,
    DestinationKind,
    ProjectionErrorCode,
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


class _Runtime:
    def __init__(
        self,
        *,
        on_project: Callable[[TaskRecord], Awaitable[None]] | None = None,
    ) -> None:
        self.records: list[TaskRecord] = []
        self._on_project = on_project

    async def project_task(self, *, record: TaskRecord) -> TaskView:
        self.records.append(record)
        if self._on_project is not None:
            await self._on_project(record)
        render = None
        if record.status in TERMINAL_STATUSES:
            render = RenderPayload(
                answer=f"安全结论 version={record.version}",
                sections=(),
                next_steps=("继续观察",),
                status=record.status,
                refs=("evidence:safe-summary",),
            )
        return TaskView(
            task_id=record.task_id,
            status=record.status,
            render=render,
            query_path=task_query_path(record.task_id),
        )


class _Messages:
    def __init__(self) -> None:
        self.chat_sends: list[tuple[str, RenderedFeishuCard, str]] = []
        self.user_sends: list[tuple[str, RenderedFeishuCard, str]] = []
        self.updates: list[tuple[str, RenderedFeishuCard]] = []
        self.failures: list[ChannelMessageError] = []
        self.before_success: Callable[[], Awaitable[None]] | None = None
        self._next_ref = 1

    async def _result(self) -> str:
        if self.failures:
            raise self.failures.pop(0)
        if self.before_success is not None:
            await self.before_success()
        result = f"message-{self._next_ref}"
        self._next_ref += 1
        return result

    async def send_to_chat(
        self,
        *,
        conversation_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        self.chat_sends.append((conversation_ref, card, idempotency_ref))
        return await self._result()

    async def send_to_user(
        self,
        *,
        subject_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        self.user_sends.append((subject_ref, card, idempotency_ref))
        return await self._result()

    async def update_card(
        self, *, message_ref: str, card: RenderedFeishuCard
    ) -> None:
        self.updates.append((message_ref, card))
        await self._result()


@dataclass
class _Harness:
    clock: object
    state: InMemoryPersistenceState
    tasks: InMemoryTaskStore
    channels: InMemoryChannelStore
    runtime: _Runtime
    messages: _Messages
    service: ChannelProjectionService


def _harness(clock, *, runtime: _Runtime | None = None) -> _Harness:
    state = InMemoryPersistenceState()
    tasks = InMemoryTaskStore(clock=clock, state=state)
    channels = InMemoryChannelStore(clock=clock, state=state)
    projection_runtime = runtime or _Runtime()
    messages = _Messages()
    service = ChannelProjectionService(
        runtime=projection_runtime,
        task_store=tasks,
        channel_store=channels,
        message_port=messages,
        clock=clock,
        settings=_Settings(),
        owner="channel-worker-test",
        sleep=asyncio.sleep,
    )
    return _Harness(
        clock=clock,
        state=state,
        tasks=tasks,
        channels=channels,
        runtime=projection_runtime,
        messages=messages,
        service=service,
    )


async def _bound_task(
    harness: _Harness,
    context,
    *,
    suffix: str,
    channel: ChannelKind = ChannelKind.FEISHU_GROUP,
    destination_kind: DestinationKind = DestinationKind.FEISHU_MESSAGE_CARD,
    initial_state: ProjectionState = ProjectionState.PENDING_INITIAL,
    destination_ref: str | None = None,
) -> tuple[TaskRecord, ProjectionSubscription]:
    task = await harness.tasks.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id=f"request-{suffix}",
                idempotency_key=f"projection-{suffix}",
                text="检查最近三十分钟慢查询",
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
                actor=context.actor,
            ),
            as_of=harness.clock(),
        )
    )
    destination = destination_ref or (
        "chat-1" if channel is ChannelKind.FEISHU_GROUP else "subject-alice"
    )
    projection = CreateProjectionSubscriptionCommand(
        task_id=task.task_id,
        destination_kind=destination_kind,
        destination_ref=destination,
        initial_state=initial_state,
        next_attempt_at=harness.clock(),
    )
    await harness.channels.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            channel=channel,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1" if channel is ChannelKind.FEISHU_GROUP else None,
            source_event_ref=f"event-{suffix}",
            created_at=harness.clock(),
            projection=projection,
        )
    )
    subscription = next(
        item
        for item in harness.state.projection_subscriptions.values()
        if item.task_id == task.task_id
    )
    return task, subscription


def _stored_subscription(
    harness: _Harness, subscription: ProjectionSubscription
) -> ProjectionSubscription:
    return harness.state.projection_subscriptions[subscription.subscription_id]


@pytest.mark.asyncio
async def test_initial_group_card_records_acceptance_then_updates_the_same_message(
    clock, context
) -> None:
    harness = _harness(clock)
    task, subscription = await _bound_task(harness, context, suffix="group")

    assert await harness.service.poll_once() == 1
    waiting = _stored_subscription(harness, subscription)
    assert waiting.state is ProjectionState.WAITING_TERMINAL
    assert waiting.source_message_ref == "message-1"
    assert waiting.provider_failure_count == 0
    assert waiting.next_attempt_at == clock() + dt.timedelta(seconds=2)
    assert len(harness.messages.chat_sends) == 1
    assert harness.messages.user_sends == []

    await drive_to_terminal(harness.tasks, lookup_for(task), TaskStatus.SUCCEEDED)
    clock.advance(seconds=2)
    assert await harness.service.poll_once() == 1

    completed = _stored_subscription(harness, subscription)
    assert completed.state is ProjectionState.COMPLETED
    assert completed.last_projected_task_version is not None
    assert completed.source_message_ref == "message-1"
    assert [item[0] for item in harness.messages.updates] == ["message-1"]


@pytest.mark.asyncio
async def test_terminal_before_initial_delivery_sends_only_the_terminal_card(
    clock, context
) -> None:
    harness = _harness(clock)
    task, subscription = await _bound_task(harness, context, suffix="already-terminal")
    await drive_to_terminal(harness.tasks, lookup_for(task), TaskStatus.FAILED)

    assert await harness.service.poll_once() == 1

    completed = _stored_subscription(harness, subscription)
    assert completed.state is ProjectionState.COMPLETED
    assert len(harness.messages.chat_sends) == 1
    assert "小维处理失败" in harness.messages.chat_sends[0][1].content_json
    assert harness.messages.updates == []


@pytest.mark.asyncio
async def test_waiting_for_task_is_not_counted_as_a_provider_failure(
    clock, context
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(
        harness,
        context,
        suffix="web-wait",
        channel=ChannelKind.WEB,
        destination_kind=DestinationKind.FEISHU_PRIVATE_NOTICE,
        initial_state=ProjectionState.WAITING_TERMINAL,
    )

    assert await harness.service.poll_once() == 1

    waiting = _stored_subscription(harness, subscription)
    assert waiting.state is ProjectionState.WAITING_TERMINAL
    assert waiting.provider_failure_count == 0
    assert waiting.last_error_code is None
    assert waiting.next_attempt_at == clock() + dt.timedelta(seconds=2)
    assert harness.messages.chat_sends == []
    assert harness.messages.user_sends == []


@pytest.mark.asyncio
async def test_web_terminal_notice_is_sent_to_the_initiating_user_once(
    clock, context
) -> None:
    harness = _harness(clock)
    task, subscription = await _bound_task(
        harness,
        context,
        suffix="web-terminal",
        channel=ChannelKind.WEB,
        destination_kind=DestinationKind.FEISHU_PRIVATE_NOTICE,
        initial_state=ProjectionState.WAITING_TERMINAL,
    )
    await drive_to_terminal(harness.tasks, lookup_for(task), TaskStatus.SUCCEEDED)

    assert await harness.service.poll_once() == 1

    assert _stored_subscription(harness, subscription).state is ProjectionState.COMPLETED
    assert [item[0] for item in harness.messages.user_sends] == ["subject-alice"]
    assert harness.messages.chat_sends == []
    assert await harness.service.poll_once() == 0


@pytest.mark.asyncio
async def test_private_feishu_card_uses_subject_not_a_guessed_chat_identifier(
    clock, context
) -> None:
    harness = _harness(clock)
    await _bound_task(
        harness,
        context,
        suffix="private",
        channel=ChannelKind.FEISHU_PRIVATE,
        destination_ref="subject-alice",
    )

    await harness.service.poll_once()

    assert [item[0] for item in harness.messages.user_sends] == ["subject-alice"]
    assert harness.messages.chat_sends == []


@pytest.mark.asyncio
async def test_worker_only_claims_its_configured_scope(clock, context) -> None:
    harness = _harness(clock)
    await _bound_task(harness, context, suffix="local")
    other = context.model_copy(update={"environment_id": "prod"})
    await _bound_task(harness, other, suffix="other-environment")

    assert await harness.service.poll_once() == 1

    assert len(harness.messages.chat_sends) == 1
    assert harness.messages.chat_sends[0][0] == "chat-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        ProjectionErrorCode.PROVIDER_RATE_LIMITED,
        ProjectionErrorCode.PROVIDER_TIMEOUT,
        ProjectionErrorCode.PROVIDER_UNAVAILABLE,
        ProjectionErrorCode.PROVIDER_INTERNAL,
    ],
)
async def test_retryable_provider_failures_use_a_separate_bounded_budget(
    clock, context, error_code: ProjectionErrorCode
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix=error_code.value)
    harness.messages.failures.append(ChannelMessageError(error_code=error_code))

    await harness.service.poll_once()

    retry = _stored_subscription(harness, subscription)
    assert retry.state is ProjectionState.PENDING_INITIAL
    assert retry.provider_failure_count == 1
    assert retry.last_error_code is error_code
    assert retry.next_attempt_at == clock() + dt.timedelta(seconds=1)


@pytest.mark.asyncio
async def test_retry_after_is_capped_without_expanding_provider_budget(
    clock, context
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix="rate-limit-cap")
    harness.messages.failures.append(
        ChannelMessageError(
            error_code=ProjectionErrorCode.PROVIDER_RATE_LIMITED,
            retry_after_seconds=300.0,
        )
    )

    await harness.service.poll_once()

    retry = _stored_subscription(harness, subscription)
    assert retry.next_attempt_at == clock() + dt.timedelta(seconds=30)
    assert retry.provider_failure_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        ProjectionErrorCode.PROVIDER_UNAUTHORIZED,
        ProjectionErrorCode.PROVIDER_FORBIDDEN,
        ProjectionErrorCode.PROVIDER_INVALID_PAYLOAD,
    ],
)
async def test_nonretryable_provider_failures_dead_letter_immediately(
    clock, context, error_code: ProjectionErrorCode
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix=error_code.value)
    harness.messages.failures.append(ChannelMessageError(error_code=error_code))

    await harness.service.poll_once()

    dead = _stored_subscription(harness, subscription)
    assert dead.state is ProjectionState.DEAD_LETTER
    assert dead.provider_failure_count == 1
    assert dead.last_error_code is error_code


@pytest.mark.asyncio
async def test_third_provider_attempt_exhausts_the_budget(clock, context) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix="retry-budget")
    harness.messages.failures.extend(
        ChannelMessageError(error_code=ProjectionErrorCode.PROVIDER_TIMEOUT)
        for _ in range(3)
    )

    for delay in (1, 2):
        await harness.service.poll_once()
        clock.advance(seconds=delay)
    await harness.service.poll_once()

    dead = _stored_subscription(harness, subscription)
    assert dead.state is ProjectionState.DEAD_LETTER
    assert dead.provider_failure_count == 3
    assert len(harness.messages.chat_sends) == 3


@pytest.mark.asyncio
async def test_version_change_is_reprojected_before_any_provider_call(
    clock, context
) -> None:
    holder: dict[str, InMemoryTaskStore] = {}

    async def advance_once(record: TaskRecord) -> None:
        if record.version != 0:
            return
        result = await holder["tasks"].transition(
            command=TransitionCommand(
                task_id=record.task_id,
                expected_version=record.version,
                to_status=TaskStatus.PLANNING,
            )
        )
        assert result.applied

    runtime = _Runtime(on_project=advance_once)
    harness = _harness(clock, runtime=runtime)
    holder["tasks"] = harness.tasks
    _, subscription = await _bound_task(harness, context, suffix="version-reread")

    await harness.service.poll_once()

    assert [record.version for record in runtime.records] == [0, 1]
    card = harness.messages.chat_sends[0][1]
    assert "任务版本：1" in card.content_json
    assert _stored_subscription(harness, subscription).last_projected_task_version == 1


@pytest.mark.asyncio
async def test_concurrent_polls_cannot_send_the_same_subscription_twice(
    clock, context
) -> None:
    harness = _harness(clock)
    await _bound_task(harness, context, suffix="same-subscription")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_first_send() -> None:
        entered.set()
        await release.wait()

    harness.messages.before_success = block_first_send
    first = asyncio.create_task(harness.service.poll_once())
    await entered.wait()
    second = asyncio.create_task(harness.service.poll_once())
    await asyncio.sleep(0)
    release.set()

    assert sorted(await asyncio.gather(first, second)) == [0, 1]
    assert len(harness.messages.chat_sends) == 1


@pytest.mark.asyncio
async def test_claim_scope_is_resolved_once_and_reused_for_projection_and_targeting(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    await _bound_task(harness, context, suffix="single-scope-read")
    original = harness.channels.resolve_claimed_task_lookup
    calls = 0

    async def counted(*, lookup):
        nonlocal calls
        calls += 1
        return await original(lookup=lookup)

    monkeypatch.setattr(harness.channels, "resolve_claimed_task_lookup", counted)

    assert await harness.service.poll_once() == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_claim_expiry_before_scope_resolution_is_a_normal_loser(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    await _bound_task(harness, context, suffix="claim-expired")
    original = harness.channels.resolve_claimed_task_lookup

    async def expire(*, lookup):
        clock.advance(seconds=15)
        return await original(lookup=lookup)

    monkeypatch.setattr(harness.channels, "resolve_claimed_task_lookup", expire)

    assert await harness.service.poll_once() == 0
    assert harness.messages.chat_sends == []
    assert harness.messages.user_sends == []


@pytest.mark.asyncio
async def test_claim_is_renewed_after_reads_before_a_slow_provider_failure(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def consume_read_budget(_: TaskRecord) -> None:
        clock.advance(seconds=14)

    harness = _harness(clock, runtime=_Runtime(on_project=consume_read_budget))
    _, subscription = await _bound_task(
        harness, context, suffix="renew-before-provider"
    )

    async def slow_rate_limit(**_: object) -> str:
        clock.advance(seconds=2)
        raise ChannelMessageError(
            error_code=ProjectionErrorCode.PROVIDER_RATE_LIMITED,
            retry_after_seconds=5,
        )

    monkeypatch.setattr(harness.messages, "send_to_chat", slow_rate_limit)

    assert await harness.service.poll_once() == 1
    retry = _stored_subscription(harness, subscription)
    assert retry.provider_failure_count == 1
    assert retry.last_error_code is ProjectionErrorCode.PROVIDER_RATE_LIMITED
    assert retry.next_attempt_at == clock() + dt.timedelta(seconds=5)


@pytest.mark.asyncio
async def test_claim_that_expires_while_renewal_returns_never_reaches_provider(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(
        harness, context, suffix="renewal-returned-expired"
    )
    original = harness.channels.renew_projection_claim

    async def expire_after_renewal(*, command):
        result = await original(command=command)
        clock.advance(seconds=15)
        return result

    monkeypatch.setattr(
        harness.channels, "renew_projection_claim", expire_after_renewal
    )

    assert await harness.service.poll_once() == 0
    assert harness.messages.chat_sends == []
    assert harness.messages.user_sends == []
    persisted = _stored_subscription(harness, subscription)
    assert persisted.provider_failure_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        ProjectionErrorCode.PROVIDER_RATE_LIMITED,
        ProjectionErrorCode.PROVIDER_FORBIDDEN,
    ],
)
async def test_provider_state_write_after_renewed_claim_expiry_is_reported_as_lost(
    clock,
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_code: ProjectionErrorCode,
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(
        harness, context, suffix="provider-outlives-renewal"
    )

    async def outlive_claim(**_: object) -> str:
        clock.advance(seconds=15)
        raise ChannelMessageError(error_code=error_code)

    monkeypatch.setattr(harness.messages, "send_to_chat", outlive_claim)

    with caplog.at_level(logging.WARNING):
        assert await harness.service.poll_once() == 0

    persisted = _stored_subscription(harness, subscription)
    assert persisted.provider_failure_count == 0
    assert persisted.state is ProjectionState.PENDING_INITIAL
    claim_lost = [
        record for record in caplog.records if record.failure_kind == "claim_lost"
    ]
    assert len(claim_lost) == 1
    assert claim_lost[0].tenant_id == "dev-local"
    assert claim_lost[0].environment_id == "dev"


@pytest.mark.asyncio
async def test_task_recheck_write_after_claim_expiry_is_reported_as_lost(
    clock,
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(
        harness,
        context,
        suffix="task-recheck-claim-lost",
        initial_state=ProjectionState.WAITING_TERMINAL,
    )
    original = harness.channels.schedule_task_recheck

    async def expire_before_write(*, command):
        clock.advance(seconds=15)
        return await original(command=command)

    monkeypatch.setattr(
        harness.channels, "schedule_task_recheck", expire_before_write
    )

    with caplog.at_level(logging.WARNING):
        assert await harness.service.poll_once() == 0

    persisted = _stored_subscription(harness, subscription)
    assert persisted.state is ProjectionState.DELIVERING_TERMINAL
    assert persisted.provider_failure_count == 0
    assert [
        record.failure_kind
        for record in caplog.records
        if getattr(record, "failure_kind", None) == "claim_lost"
    ] == ["claim_lost"]


@pytest.mark.asyncio
async def test_terminal_completion_write_after_claim_expiry_is_reported_as_lost(
    clock,
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _harness(clock)
    task, subscription = await _bound_task(
        harness, context, suffix="completion-claim-lost"
    )
    assert await harness.service.poll_once() == 1
    await drive_to_terminal(harness.tasks, lookup_for(task), TaskStatus.SUCCEEDED)
    clock.advance(seconds=2)

    async def outlive_claim(**_: object) -> None:
        clock.advance(seconds=15)

    monkeypatch.setattr(harness.messages, "update_card", outlive_claim)

    with caplog.at_level(logging.WARNING):
        assert await harness.service.poll_once() == 0

    persisted = _stored_subscription(harness, subscription)
    assert persisted.state is ProjectionState.DELIVERING_TERMINAL
    assert persisted.source_message_ref == "message-1"
    assert [
        record.failure_kind
        for record in caplog.records
        if getattr(record, "failure_kind", None) == "claim_lost"
    ] == ["claim_lost"]


@pytest.mark.asyncio
async def test_group_binding_storage_failure_is_not_charged_to_provider_budget(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(
        harness, context, suffix="binding-storage-failure"
    )

    async def fail_binding_read(**_: object):
        raise RuntimeError("database failure must remain infrastructure failure")

    monkeypatch.setattr(harness.channels, "get_group_binding", fail_binding_read)

    with pytest.raises(
        RuntimeError, match="database failure must remain infrastructure failure"
    ):
        await harness.service.poll_once()

    persisted = _stored_subscription(harness, subscription)
    assert persisted.provider_failure_count == 0
    assert persisted.last_error_code is None
    assert harness.messages.chat_sends == []
    assert harness.messages.user_sends == []


@pytest.mark.asyncio
async def test_untyped_message_port_failure_remains_a_sanitized_provider_failure(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(
        harness, context, suffix="untyped-provider-failure"
    )

    async def fail_provider(**_: object) -> str:
        raise RuntimeError("provider response body must not escape")

    monkeypatch.setattr(harness.messages, "send_to_chat", fail_provider)

    assert await harness.service.poll_once() == 1
    persisted = _stored_subscription(harness, subscription)
    assert persisted.provider_failure_count == 1
    assert persisted.last_error_code is ProjectionErrorCode.PROVIDER_INTERNAL


@pytest.mark.asyncio
async def test_group_binding_drift_fails_stop_instead_of_becoming_provider_failure(
    clock, context
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix="binding-drift")
    binding_id, binding = next(iter(harness.state.channel_bindings.items()))
    harness.state.channel_bindings[binding_id] = binding.model_copy(
        update={"conversation_ref": "chat-other"}
    )

    with pytest.raises(
        ChannelProjectionInvariantError,
        match="destination differs from its group binding",
    ):
        await harness.service.poll_once()

    persisted = _stored_subscription(harness, subscription)
    assert persisted.provider_failure_count == 0
    assert persisted.last_error_code is None
    assert harness.messages.chat_sends == []
    assert harness.messages.user_sends == []


@pytest.mark.asyncio
async def test_task_poll_backoff_is_not_inflated_by_provider_failures(
    clock, context
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix="separate-budgets")
    harness.messages.failures.extend(
        ChannelMessageError(error_code=ProjectionErrorCode.PROVIDER_TIMEOUT)
        for _ in range(2)
    )

    for delay in (1, 2):
        await harness.service.poll_once()
        clock.advance(seconds=delay)
    await harness.service.poll_once()
    waiting = _stored_subscription(harness, subscription)
    assert waiting.provider_failure_count == 2
    assert waiting.next_attempt_at == clock() + dt.timedelta(seconds=2)

    clock.advance(seconds=2)
    await harness.service.poll_once()
    waiting = _stored_subscription(harness, subscription)
    assert waiting.next_attempt_at == clock() + dt.timedelta(seconds=4)


@pytest.mark.asyncio
async def test_empty_provider_message_reference_is_a_sanitized_retryable_failure(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    _, subscription = await _bound_task(harness, context, suffix="empty-message-ref")

    async def empty_reference(**_: object) -> str:
        return ""

    monkeypatch.setattr(harness.messages, "send_to_chat", empty_reference)

    assert await harness.service.poll_once() == 1
    retry = _stored_subscription(harness, subscription)
    assert retry.state is ProjectionState.PENDING_INITIAL
    assert retry.provider_failure_count == 1
    assert retry.last_error_code is ProjectionErrorCode.PROVIDER_INTERNAL


def test_message_error_requires_a_real_closed_taxonomy_member() -> None:
    with pytest.raises(TypeError, match="ProjectionErrorCode"):
        ChannelMessageError(error_code="provider_new_code")


def test_task_poll_backoff_caps_before_unbounded_exponentiation(
    clock, context
) -> None:
    harness = _harness(clock)
    subscription = ProjectionSubscription(
        subscription_id="subscription-high-attempt",
        task_id="task-high-attempt",
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref="chat-1",
        state=ProjectionState.WAITING_TERMINAL,
        attempt_number=10_000,
        next_attempt_at=clock(),
        provider_failure_count=0,
    )

    assert harness.service._task_poll_delay(subscription) == 10.0


@pytest.mark.asyncio
async def test_one_fatal_projection_cancels_sibling_work_before_poll_fails(
    clock, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(clock)
    await _bound_task(harness, context, suffix="fatal-first")
    await _bound_task(harness, context, suffix="blocked-sibling")
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    calls = 0
    blocked_task: asyncio.Task[object] | None = None

    async def process(_: ProjectionSubscription) -> int:
        nonlocal calls, blocked_task
        calls += 1
        if calls == 1:
            await sibling_started.wait()
            raise RuntimeError("constant projection invariant failure")
        blocked_task = asyncio.current_task()
        sibling_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    monkeypatch.setattr(harness.service, "_process_due", process)
    try:
        with pytest.raises(RuntimeError, match="constant projection invariant failure"):
            await harness.service.poll_once()
        await asyncio.wait_for(sibling_cancelled.wait(), timeout=1)
    finally:
        if blocked_task is not None and not blocked_task.done():
            blocked_task.cancel()
            await asyncio.gather(blocked_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_worker_loop_awaits_its_internal_wait_tasks(
    clock, context
) -> None:
    sleep_started = asyncio.Event()
    sleep_cancelled = asyncio.Event()

    async def blocking_sleep(_: float) -> None:
        sleep_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            sleep_cancelled.set()
            raise

    harness = _harness(clock)
    harness.service._sleep = blocking_sleep
    running = asyncio.create_task(harness.service.run(asyncio.Event()))
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert sleep_cancelled.is_set()

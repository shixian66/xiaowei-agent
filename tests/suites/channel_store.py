"""``ChannelStore`` 的跨实现行为用例。"""

import datetime as dt
from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest
from tests.conftest import make_envelope, make_submission

from xiaowei_agent.contracts import (
    ChannelKind,
    DestinationKind,
    ProjectionErrorCode,
    ProjectionState,
)
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    ChannelBindingConflictError,
    ChannelBindingNotFoundError,
    ClaimedTaskLookup,
    ClaimProjectionCommand,
    CompleteProjectionCommand,
    CreateProjectionSubscriptionCommand,
    DeadLetterProjectionCommand,
    GroupBindingLookup,
    GroupBoundTaskIdsQuery,
    ProjectionClaimNotFoundError,
    ProjectionDueQuery,
    ProjectionSubscriptionConflictError,
    RecordInitialProjectionCommand,
    ScheduleProviderRetryCommand,
    ScheduleTaskRecheckCommand,
)
from xiaowei_agent.persistence.store import TaskNotFoundError


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    for case in cases:
        namespace[case.__name__] = case


async def _task(store: Any, context: Any, suffix: str) -> Any:
    return await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id=f"request-{suffix}",
                idempotency_key=f"channel-{suffix}",
                text=f"channel request {suffix}",
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
                actor=context.actor,
            ),
        )
    )


def _subscription(
    task_id: str,
    now: dt.datetime,
    *,
    destination_ref: str = "chat-1",
    initial_state: ProjectionState = ProjectionState.PENDING_INITIAL,
) -> CreateProjectionSubscriptionCommand:
    return CreateProjectionSubscriptionCommand(
        task_id=task_id,
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref=destination_ref,
        initial_state=initial_state,
        next_attempt_at=now,
    )


def _binding(
    task_id: str,
    now: dt.datetime,
    *,
    source_event_ref: str = "event-digest-1",
    channel: ChannelKind = ChannelKind.FEISHU_GROUP,
    projection: CreateProjectionSubscriptionCommand | None = None,
    tenant_id: str = "dev-local",
    environment_id: str = "dev",
) -> BindTaskCommand:
    return BindTaskCommand(
        task_id=task_id,
        tenant_id=tenant_id,
        environment_id=environment_id,
        channel=channel,
        initiator_subject_ref="subject-alice",
        conversation_ref="chat-1" if channel is ChannelKind.FEISHU_GROUP else None,
        source_event_ref=source_event_ref,
        created_at=now,
        projection=projection,
    )


async def test_binding_is_idempotent_and_atomically_recovers_its_subscription(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "bind-idempotent")
    command = _binding(
        task.task_id,
        clock(),
        projection=_subscription(task.task_id, clock()),
    )

    first = await channel_store.bind_task(command=command)
    second = await channel_store.bind_task(command=command)
    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )

    assert second == first
    assert [item.task_id for item in due] == [task.task_id]


async def test_same_source_event_cannot_be_rebound_to_a_different_task(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    first_task = await _task(store, context, "binding-first")
    second_task = await _task(store, context, "binding-second")
    await channel_store.bind_task(command=_binding(first_task.task_id, clock()))

    with pytest.raises(ChannelBindingConflictError):
        await channel_store.bind_task(command=_binding(second_task.task_id, clock()))


async def test_binding_cannot_override_the_task_store_scope(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "binding-wrong-scope")
    command = _binding(task.task_id, clock()).model_copy(
        update={"tenant_id": "other-tenant"}
    )

    with pytest.raises(TaskNotFoundError):
        await channel_store.bind_task(command=command)

    with pytest.raises(ChannelBindingNotFoundError):
        await channel_store.get_group_binding(
            lookup=GroupBindingLookup(
                task_id=task.task_id,
                tenant_id="dev-local",
                environment_id="dev",
            )
        )


async def test_binding_rolls_back_when_its_nested_subscription_conflicts(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "binding-rollback")
    await channel_store.create_projection_subscription(
        command=_subscription(
            task.task_id,
            clock(),
            initial_state=ProjectionState.WAITING_TERMINAL,
        )
    )

    with pytest.raises(ProjectionSubscriptionConflictError):
        await channel_store.bind_task(
            command=_binding(
                task.task_id,
                clock(),
                projection=_subscription(task.task_id, clock()),
            )
        )

    with pytest.raises(ChannelBindingNotFoundError):
        await channel_store.get_group_binding(
            lookup=GroupBindingLookup(
                task_id=task.task_id,
                tenant_id="dev-local",
                environment_id="dev",
            )
        )


async def test_group_binding_lookup_is_scope_hidden_and_excludes_private_bindings(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    group_task = await _task(store, context, "group-lookup")
    private_task = await _task(store, context, "private-lookup")
    group = await channel_store.bind_task(command=_binding(group_task.task_id, clock()))
    await channel_store.bind_task(
        command=_binding(
            private_task.task_id,
            clock(),
            source_event_ref="event-private",
            channel=ChannelKind.FEISHU_PRIVATE,
        )
    )

    assert (
        await channel_store.get_group_binding(
            lookup=GroupBindingLookup(
                task_id=group_task.task_id,
                tenant_id="dev-local",
                environment_id="dev",
            )
        )
        == group
    )
    for lookup in (
        GroupBindingLookup(
            task_id=group_task.task_id,
            tenant_id="other-tenant",
            environment_id="dev",
        ),
        GroupBindingLookup(
            task_id=private_task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
        ),
    ):
        with pytest.raises(ChannelBindingNotFoundError):
            await channel_store.get_group_binding(lookup=lookup)


async def test_group_bound_task_ids_are_batched_and_scope_hidden(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    group_task = await _task(store, context, "group-batch")
    private_task = await _task(store, context, "private-batch")
    other_tenant_context = context.model_copy(
        update={"tenant_id": "other-tenant"}
    )
    other_environment_context = context.model_copy(
        update={"environment_id": "prod"}
    )
    other_tenant_task = await _task(
        store, other_tenant_context, "other-tenant-group-batch"
    )
    other_environment_task = await _task(
        store, other_environment_context, "other-environment-group-batch"
    )
    await channel_store.bind_task(
        command=_binding(
            group_task.task_id,
            clock(),
            source_event_ref="event-group-batch",
        )
    )
    await channel_store.bind_task(
        command=_binding(
            private_task.task_id,
            clock(),
            source_event_ref="event-private-batch",
            channel=ChannelKind.FEISHU_PRIVATE,
        )
    )
    await channel_store.bind_task(
        command=_binding(
            other_tenant_task.task_id,
            clock(),
            source_event_ref="event-other-tenant-group-batch",
            tenant_id="other-tenant",
        )
    )
    await channel_store.bind_task(
        command=_binding(
            other_environment_task.task_id,
            clock(),
            source_event_ref="event-other-environment-group-batch",
            environment_id="prod",
        )
    )

    task_ids = await channel_store.list_group_bound_task_ids(
        query=GroupBoundTaskIdsQuery(
            tenant_id="dev-local",
            environment_id="dev",
            task_ids=(
                group_task.task_id,
                private_task.task_id,
                other_tenant_task.task_id,
                other_environment_task.task_id,
                "missing-task",
            ),
        )
    )

    assert task_ids == frozenset({group_task.task_id})


async def test_projection_creation_is_idempotent_but_semantic_conflicts_fail_closed(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "projection-idempotent")
    command = _subscription(task.task_id, clock())

    first = await channel_store.create_projection_subscription(command=command)
    second = await channel_store.create_projection_subscription(command=command)
    assert second == first

    with pytest.raises(ProjectionSubscriptionConflictError):
        await channel_store.create_projection_subscription(
            command=command.model_copy(
                update={"initial_state": ProjectionState.WAITING_TERMINAL}
            )
        )


async def test_projection_subscription_cannot_reference_an_unknown_task(
    channel_store: Any, clock: Any
) -> None:
    with pytest.raises(TaskNotFoundError):
        await channel_store.create_projection_subscription(
            command=_subscription("missing-task", clock())
        )


async def test_due_scan_is_stable_and_ignores_future_or_live_claims(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    first_task = await _task(store, context, "due-first")
    second_task = await _task(store, context, "due-second")
    future_task = await _task(store, context, "due-future")
    first = await channel_store.create_projection_subscription(
        command=_subscription(first_task.task_id, clock(), destination_ref="chat-a")
    )
    second = await channel_store.create_projection_subscription(
        command=_subscription(second_task.task_id, clock(), destination_ref="chat-b")
    )
    await channel_store.create_projection_subscription(
        command=_subscription(future_task.task_id, clock(), destination_ref="chat-c").model_copy(
            update={"next_attempt_at": clock() + dt.timedelta(seconds=60)}
        )
    )
    for task, source_event_ref in (
        (first_task, "event-due-first"),
        (second_task, "event-due-second"),
        (future_task, "event-due-future"),
    ):
        await channel_store.bind_task(
            command=_binding(
                task.task_id,
                clock(),
                source_event_ref=source_event_ref,
            )
        )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=first.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    assert claimed.applied

    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )
    assert due == (second,)


async def test_due_scan_returns_only_the_requested_tenant_and_environment(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    local_task = await _task(store, context, "due-local-scope")
    other_tenant_context = context.model_copy(
        update={"tenant_id": "other-tenant"}
    )
    other_environment_context = context.model_copy(
        update={"environment_id": "prod"}
    )
    other_tenant_task = await _task(
        store, other_tenant_context, "due-other-tenant-scope"
    )
    other_environment_task = await _task(
        store, other_environment_context, "due-other-environment-scope"
    )
    unbound_task = await _task(store, context, "due-unbound-scope")
    await channel_store.bind_task(
        command=_binding(
            local_task.task_id,
            clock(),
            source_event_ref="event-due-local-scope",
            projection=_subscription(local_task.task_id, clock()),
        )
    )
    await channel_store.bind_task(
        command=_binding(
            other_tenant_task.task_id,
            clock(),
            source_event_ref="event-due-other-tenant-scope",
            projection=_subscription(
                other_tenant_task.task_id,
                clock(),
                destination_ref="chat-other-tenant",
            ),
            tenant_id="other-tenant",
        )
    )
    await channel_store.bind_task(
        command=_binding(
            other_environment_task.task_id,
            clock(),
            source_event_ref="event-due-other-environment-scope",
            projection=_subscription(
                other_environment_task.task_id,
                clock(),
                destination_ref="chat-other-environment",
            ),
            environment_id="prod",
        )
    )
    await channel_store.create_projection_subscription(
        command=_subscription(
            unbound_task.task_id,
            clock(),
            destination_ref="chat-unbound",
        )
    )

    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local",
            environment_id="dev",
            limit=100,
        )
    )

    assert [item.task_id for item in due] == [local_task.task_id]


async def test_expired_claim_gets_a_higher_fence_and_stale_worker_cannot_commit(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "claim-fencing")
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(
            task.task_id,
            clock(),
            initial_state=ProjectionState.WAITING_TERMINAL,
        )
    )
    first = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.WAITING_TERMINAL,
        )
    )
    assert first.applied and first.winner.fencing_token is not None
    assert first.winner.state is ProjectionState.DELIVERING_TERMINAL

    clock.advance(seconds=30)
    second = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            # 复用同一 worker 名，确保旧提交只能由 fencing token 被拒绝，
            # 不是被 owner 不同这条更早的条件碰巧挡住。
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.DELIVERING_TERMINAL,
        )
    )
    assert second.applied and second.winner.fencing_token is not None
    assert second.winner.fencing_token > first.winner.fencing_token

    stale = await channel_store.schedule_task_recheck(
        command=ScheduleTaskRecheckCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            fencing_token=first.winner.fencing_token,
            expected_state=ProjectionState.DELIVERING_TERMINAL,
            next_attempt_at=clock() + dt.timedelta(seconds=5),
        )
    )
    assert not stale.applied
    assert stale.winner.fencing_token == second.winner.fencing_token


async def test_only_a_live_projection_claim_can_resolve_its_task_scope(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "claimed-scope")
    await channel_store.bind_task(
        command=_binding(
            task.task_id,
            clock(),
            projection=_subscription(task.task_id, clock()),
        )
    )
    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=due[0].subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    assert claimed.winner.fencing_token is not None
    lookup = ClaimedTaskLookup(
        subscription_id=claimed.winner.subscription_id,
        claim_owner="worker-a",
        fencing_token=claimed.winner.fencing_token,
    )

    resolved = await channel_store.resolve_claimed_task_lookup(lookup=lookup)

    assert resolved.task_id == task.task_id
    assert resolved.tenant_id == "dev-local"
    assert resolved.environment_id == "dev"

    for invalid in (
        lookup.model_copy(update={"claim_owner": "worker-b"}),
        lookup.model_copy(update={"fencing_token": lookup.fencing_token + 1}),
    ):
        with pytest.raises(ProjectionClaimNotFoundError):
            await channel_store.resolve_claimed_task_lookup(lookup=invalid)

    clock.advance(seconds=30)
    with pytest.raises(ProjectionClaimNotFoundError):
        await channel_store.resolve_claimed_task_lookup(lookup=lookup)


async def test_claim_without_a_channel_binding_cannot_resolve_task_scope(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "unbound-claim")
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(task.task_id, clock())
    )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    assert claimed.winner.fencing_token is not None

    with pytest.raises(ProjectionClaimNotFoundError):
        await channel_store.resolve_claimed_task_lookup(
            lookup=ClaimedTaskLookup(
                subscription_id=subscription.subscription_id,
                claim_owner="worker-a",
                fencing_token=claimed.winner.fencing_token,
            )
        )


async def test_task_wait_releases_claim_without_counting_a_provider_failure(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "task-wait")
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(
            task.task_id,
            clock(),
            initial_state=ProjectionState.WAITING_TERMINAL,
        )
    )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.WAITING_TERMINAL,
        )
    )
    winner = claimed.winner
    result = await channel_store.schedule_task_recheck(
        command=ScheduleTaskRecheckCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            fencing_token=winner.fencing_token,
            expected_state=ProjectionState.DELIVERING_TERMINAL,
            next_attempt_at=clock() + dt.timedelta(seconds=5),
        )
    )

    assert result.applied
    assert result.winner.state is ProjectionState.WAITING_TERMINAL
    assert result.winner.provider_failure_count == 0
    assert result.winner.last_error_code is None
    assert result.winner.claim_owner is None


async def test_initial_projection_and_provider_retry_follow_separate_paths(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "initial-projection")
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(task.task_id, clock())
    )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    token = claimed.winner.fencing_token
    retry = await channel_store.schedule_provider_retry(
        command=ScheduleProviderRetryCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            fencing_token=token,
            expected_state=ProjectionState.PENDING_INITIAL,
            next_attempt_at=clock() + dt.timedelta(seconds=1),
            error_code=ProjectionErrorCode.PROVIDER_TIMEOUT,
        )
    )
    assert retry.applied
    assert retry.winner.provider_failure_count == 1
    assert retry.winner.last_error_code is ProjectionErrorCode.PROVIDER_TIMEOUT

    clock.advance(seconds=1)
    reclaimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-b",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    recorded = await channel_store.record_initial_projection(
        command=RecordInitialProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-b",
            fencing_token=reclaimed.winner.fencing_token,
            expected_state=ProjectionState.PENDING_INITIAL,
            source_message_ref="message-1",
            task_version=0,
            payload_digest="a" * 64,
            task_terminal=False,
            next_attempt_at=clock() + dt.timedelta(seconds=5),
        )
    )
    assert recorded.applied
    assert recorded.winner.state is ProjectionState.WAITING_TERMINAL
    assert recorded.winner.source_message_ref == "message-1"
    assert recorded.winner.provider_failure_count == 1


async def test_completed_projection_is_terminal_against_claims_and_late_updates(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "projection-terminal")
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(task.task_id, clock())
    )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    completed = await channel_store.complete_projection(
        command=CompleteProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            fencing_token=claimed.winner.fencing_token,
            expected_state=ProjectionState.PENDING_INITIAL,
            source_message_ref="message-terminal",
            task_version=1,
            payload_digest="b" * 64,
        )
    )
    assert completed.applied
    assert completed.winner.state is ProjectionState.COMPLETED

    late_claim = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-b",
            ttl_seconds=30,
            expected_state=ProjectionState.COMPLETED,
        )
    )
    late_dead_letter = await channel_store.dead_letter_projection(
        command=DeadLetterProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            fencing_token=claimed.winner.fencing_token,
            expected_state=ProjectionState.COMPLETED,
            error_code=ProjectionErrorCode.PROVIDER_INTERNAL,
        )
    )
    assert not late_claim.applied
    assert not late_dead_letter.applied
    assert late_dead_letter.winner == completed.winner


async def test_dead_letter_projection_is_terminal_against_reclaim(
    channel_store: Any, store: Any, context: Any, clock: Any
) -> None:
    task = await _task(store, context, "projection-dead-letter")
    subscription = await channel_store.create_projection_subscription(
        command=_subscription(task.task_id, clock())
    )
    claimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            ttl_seconds=30,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    dead = await channel_store.dead_letter_projection(
        command=DeadLetterProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-a",
            fencing_token=claimed.winner.fencing_token,
            expected_state=ProjectionState.PENDING_INITIAL,
            error_code=ProjectionErrorCode.PROVIDER_FORBIDDEN,
        )
    )

    assert dead.applied
    assert dead.winner.state is ProjectionState.DEAD_LETTER
    assert dead.winner.provider_failure_count == 1
    reclaimed = await channel_store.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-b",
            ttl_seconds=30,
            expected_state=ProjectionState.DEAD_LETTER,
        )
    )
    assert not reclaimed.applied
    assert reclaimed.winner == dead.winner


CHANNEL_STORE_CASES = (
    test_binding_is_idempotent_and_atomically_recovers_its_subscription,
    test_same_source_event_cannot_be_rebound_to_a_different_task,
    test_binding_cannot_override_the_task_store_scope,
    test_binding_rolls_back_when_its_nested_subscription_conflicts,
    test_group_binding_lookup_is_scope_hidden_and_excludes_private_bindings,
    test_group_bound_task_ids_are_batched_and_scope_hidden,
    test_projection_creation_is_idempotent_but_semantic_conflicts_fail_closed,
    test_projection_subscription_cannot_reference_an_unknown_task,
    test_due_scan_is_stable_and_ignores_future_or_live_claims,
    test_due_scan_returns_only_the_requested_tenant_and_environment,
    test_expired_claim_gets_a_higher_fence_and_stale_worker_cannot_commit,
    test_only_a_live_projection_claim_can_resolve_its_task_scope,
    test_claim_without_a_channel_binding_cannot_resolve_task_scope,
    test_task_wait_releases_claim_without_counting_a_provider_failure,
    test_initial_projection_and_provider_retry_follow_separate_paths,
    test_completed_projection_is_terminal_against_claims_and_late_updates,
    test_dead_letter_projection_is_terminal_against_reclaim,
)

ALL_GROUPS = {"channel_store": CHANNEL_STORE_CASES}

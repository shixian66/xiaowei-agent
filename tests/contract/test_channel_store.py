"""M7 ChannelStore 契约 —— 内存实现绑定与行映射。"""

import datetime as dt

import pytest
from pydantic import ValidationError
from tests.suites.channel_store import CHANNEL_STORE_CASES, bind

from xiaowei_agent.contracts import (
    ChannelKind,
    DestinationKind,
    ProjectionErrorCode,
    ProjectionState,
)
from xiaowei_agent.persistence.channel import (
    ChannelBinding,
    CreateProjectionSubscriptionCommand,
    GroupBoundTaskIdsQuery,
    ProjectionDueQuery,
    ProjectionSubscription,
)
from xiaowei_agent.persistence.fake import InMemoryChannelStore
from xiaowei_agent.persistence.rows import (
    channel_binding_to_row,
    projection_subscription_to_row,
    row_to_channel_binding,
    row_to_projection_subscription,
)


@pytest.fixture
def channel_store(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


bind(globals(), CHANNEL_STORE_CASES)


_NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)


def test_worker_queries_require_explicit_scope_and_bounded_batches() -> None:
    assert set(ProjectionDueQuery.model_fields) == {
        "tenant_id",
        "environment_id",
        "limit",
    }
    assert set(GroupBoundTaskIdsQuery.model_fields) == {
        "tenant_id",
        "environment_id",
        "task_ids",
    }

    with pytest.raises(ValidationError):
        ProjectionDueQuery(limit=100)
    with pytest.raises(ValidationError):
        GroupBoundTaskIdsQuery(
            tenant_id="tenant-a", environment_id="dev", task_ids=()
        )
    with pytest.raises(ValidationError):
        GroupBoundTaskIdsQuery(
            tenant_id="tenant-a",
            environment_id="dev",
            task_ids=tuple(f"task-{index}" for index in range(101)),
        )


def test_channel_binding_row_mapping_round_trips_without_extra_fields() -> None:
    binding = ChannelBinding(
        binding_id="binding-1",
        task_id="task-1",
        tenant_id="tenant-a",
        environment_id="dev",
        channel=ChannelKind.FEISHU_GROUP,
        initiator_subject_ref="subject-1",
        conversation_ref="chat-1",
        source_event_ref="event-digest-1",
        created_at=_NOW,
    )

    row = channel_binding_to_row(binding)

    assert set(row) == set(ChannelBinding.model_fields)
    assert row_to_channel_binding(row) == binding


def test_projection_subscription_row_mapping_round_trips_claim_fields() -> None:
    subscription = ProjectionSubscription(
        subscription_id="subscription-1",
        task_id="task-1",
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref="chat-1",
        state=ProjectionState.DELIVERING_TERMINAL,
        last_projected_task_version=2,
        attempt_number=3,
        claim_owner="worker-1",
        claim_expires_at=_NOW + dt.timedelta(seconds=30),
        fencing_token=7,
        next_attempt_at=_NOW,
        provider_failure_count=0,
        payload_digest="a" * 64,
    )

    row = projection_subscription_to_row(subscription)

    assert set(row) == set(ProjectionSubscription.model_fields)
    assert row_to_projection_subscription(row) == subscription


def test_group_binding_and_completed_projection_require_their_delivery_refs() -> None:
    with pytest.raises(ValidationError, match="group binding requires conversation_ref"):
        ChannelBinding(
            binding_id="binding-1",
            task_id="task-1",
            tenant_id="tenant-a",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-1",
            conversation_ref=None,
            source_event_ref="event-1",
            created_at=_NOW,
        )

    with pytest.raises(ValidationError, match="completed projection must record"):
        ProjectionSubscription(
            subscription_id="subscription-1",
            task_id="task-1",
            destination_kind=DestinationKind.FEISHU_PRIVATE_NOTICE,
            destination_ref="subject-1",
            source_message_ref=None,
            state=ProjectionState.COMPLETED,
            last_projected_task_version=1,
            attempt_number=1,
            next_attempt_at=_NOW,
            provider_failure_count=0,
            payload_digest="a" * 64,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"claim_owner": "worker-1"}, "claim fields must be set or cleared"),
        (
            {"last_projected_task_version": 1},
            "projection version and payload digest must be set or cleared",
        ),
        (
            {"last_error_code": ProjectionErrorCode.PROVIDER_TIMEOUT},
            "provider failure count and error code must agree",
        ),
        (
            {
                "state": ProjectionState.DEAD_LETTER,
                "claim_owner": "worker-1",
                "claim_expires_at": _NOW + dt.timedelta(seconds=30),
                "fencing_token": 1,
            },
            "terminal projection cannot retain a claim",
        ),
    ],
)
def test_projection_subscription_rejects_partial_or_terminal_inconsistent_state(
    updates, message
) -> None:
    fields = {
        "subscription_id": "subscription-1",
        "task_id": "task-1",
        "destination_kind": DestinationKind.FEISHU_MESSAGE_CARD,
        "destination_ref": "chat-1",
        "state": ProjectionState.PENDING_INITIAL,
        "attempt_number": 0,
        "next_attempt_at": _NOW,
        "provider_failure_count": 0,
        **updates,
    }

    with pytest.raises(ValidationError, match=message):
        ProjectionSubscription(**fields)


def test_projection_subscription_can_only_start_in_a_schedulable_state() -> None:
    with pytest.raises(ValidationError, match="must start pending or waiting"):
        CreateProjectionSubscriptionCommand(
            task_id="task-1",
            destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
            destination_ref="chat-1",
            initial_state=ProjectionState.COMPLETED,
            next_attempt_at=_NOW,
        )

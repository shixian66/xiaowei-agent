"""M7 渠道提交服务：权限、服务端幂等、绑定恢复与通知订阅。"""

from typing import Any

import pytest

from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
    channel_idempotency_key,
    derive_channel_submission_references,
)
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    IdentitySource,
    ProjectionState,
)
from xiaowei_agent.persistence import IdempotencyConflictError
from xiaowei_agent.persistence.channel import (
    ChannelBindingConflictError,
    ProjectionDueQuery,
)
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryChannelStore
from xiaowei_agent.persistence.plans import InMemoryPlanStore


def _principal(
    *,
    actor: str = "alice",
    tenant_id: str = "dev-local",
    environment_id: str = "dev",
    permissions: frozenset[ChannelPermission] | None = None,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id=tenant_id,
        environment_id=environment_id,
        actor=actor,
        source=IdentitySource.FEISHU,
        subject_ref=f"subject-{actor}",
        permissions=permissions
        if permissions is not None
        else frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    )


def _command(
    clock: Any,
    *,
    principal: AuthenticatedPrincipal | None = None,
    channel: ChannelKind = ChannelKind.FEISHU_GROUP,
    client_key: str = "event-1",
    text: str = "inspect slow queries",
) -> ChannelSubmitCommand:
    return ChannelSubmitCommand(
        principal=_principal() if principal is None else principal,
        channel=channel,
        request_id=f"request-{client_key}",
        trace_id="1" * 32,
        policy_revision="policy-1",
        text=text,
        client_submission_ref=client_key,
        conversation_ref="chat-1" if channel is ChannelKind.FEISHU_GROUP else None,
        submitted_at=clock(),
    )


def test_group_submission_requires_a_conversation_reference(clock) -> None:
    with pytest.raises(ValueError, match="group submission requires conversation_ref"):
        _command(clock).model_copy(update={"conversation_ref": None})


@pytest.fixture
def channel_store(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


@pytest.fixture
def service(store, channel_store, memory_state):
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
    )
    return ChannelSubmissionService(runtime=runtime, channel_store=channel_store)


async def test_submit_requires_the_explicit_readonly_submission_permission(
    service, clock
) -> None:
    principal = _principal(permissions=frozenset({ChannelPermission.VIEW_SAFE_TASK}))

    with pytest.raises(ChannelSubmissionForbiddenError, match="submission forbidden"):
        await service.submit(command=_command(clock, principal=principal))


async def test_same_client_key_replays_one_runtime_task_and_one_binding(
    service, channel_store, clock
) -> None:
    command = _command(clock)

    first = await service.submit(command=command)
    second = await service.submit(command=command.model_copy(update={"request_id": "retry-2"}))
    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )

    assert second == first
    assert len(due) == 1
    assert due[0].task_id == first.task_view.task_id
    assert due[0].state is ProjectionState.PENDING_INITIAL


async def test_same_scoped_client_key_with_different_text_keeps_runtime_conflict(
    service, clock
) -> None:
    await service.submit(command=_command(clock, text="request one"))

    with pytest.raises(IdempotencyConflictError):
        await service.submit(command=_command(clock, text="request two"))


async def test_same_event_cannot_be_replayed_from_a_different_conversation(
    service, clock
) -> None:
    command = _command(clock)
    await service.submit(command=command)

    with pytest.raises(ChannelBindingConflictError):
        await service.submit(
            command=command.model_copy(
                update={"request_id": "retry-other-chat", "conversation_ref": "chat-2"}
            )
        )


async def test_web_user_gets_terminal_notice_but_admin_gets_no_subscription(
    service, channel_store, clock
) -> None:
    ordinary = await service.submit(
        command=_command(clock, channel=ChannelKind.WEB, client_key="web-user")
    )
    admin_principal = _principal(
        actor="root",
        permissions=frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
                ChannelPermission.ADMIN_ALL_SAFE_TASKS,
            }
        ),
    )
    admin = await service.submit(
        command=_command(
            clock,
            principal=admin_principal,
            channel=ChannelKind.WEB,
            client_key="web-admin",
        )
    )
    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )

    assert [item.task_id for item in due] == [ordinary.task_view.task_id]
    assert due[0].state is ProjectionState.WAITING_TERMINAL
    assert admin.task_view.task_id not in {item.task_id for item in due}


def test_server_idempotency_key_is_scoped_by_every_authority_dimension(clock) -> None:
    base = _command(clock)
    variants = (
        base.model_copy(update={"principal": _principal(actor="bob")}),
        base.model_copy(update={"principal": _principal(tenant_id="other-tenant")}),
        base.model_copy(update={"principal": _principal(environment_id="prod")}),
        base.model_copy(update={"channel": ChannelKind.FEISHU_PRIVATE, "conversation_ref": None}),
        base.model_copy(update={"client_submission_ref": "event-2"}),
    )

    keys = {channel_idempotency_key(base), *(channel_idempotency_key(item) for item in variants)}
    assert len(keys) == len(variants) + 1
    assert all(key.startswith("channel:v1:") and len(key) == 75 for key in keys)


def test_runtime_key_and_source_event_digest_are_derived_together(clock) -> None:
    base = _command(clock, client_key="same-provider-event")
    retry = base.model_copy(
        update={
            "request_id": "retry-request",
            "trace_id": "2" * 32,
            "text": "different body is checked by TaskStore",
        }
    )
    other_actor = base.model_copy(update={"principal": _principal(actor="bob")})

    first = derive_channel_submission_references(base)
    replay = derive_channel_submission_references(retry)
    other = derive_channel_submission_references(other_actor)

    assert replay == first
    assert other != first
    assert first.idempotency_key == f"channel:v1:{first.source_event_ref}"
    assert len(first.source_event_ref) == 64
    assert set(first.source_event_ref) <= set("0123456789abcdef")
    assert base.client_submission_ref not in first.source_event_ref


async def test_same_provider_reference_is_isolated_between_actors(service, clock) -> None:
    first = await service.submit(
        command=_command(clock, client_key="shared-provider-reference")
    )
    second = await service.submit(
        command=_command(
            clock,
            principal=_principal(actor="bob"),
            client_key="shared-provider-reference",
        )
    )

    assert first.task_view.task_id != second.task_view.task_id
    assert first.binding.source_event_ref != second.binding.source_event_ref
    assert len(first.binding.source_event_ref) == 64
    assert len(second.binding.source_event_ref) == 64


async def test_binding_failure_leaves_one_recoverable_runtime_task(
    store, channel_store, memory_state, clock
) -> None:
    class FailFirstBinding:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate
            self.calls = 0

        async def bind_task(self, *, command: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise PersistenceUnavailableError(
                    category=PersistenceUnavailableCategory.TRANSIENT,
                    write_outcome=PersistenceWriteOutcome.ROLLED_BACK,
                )
            return await self.delegate.bind_task(command=command)

    flaky = FailFirstBinding(channel_store)
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
    )
    service = ChannelSubmissionService(runtime=runtime, channel_store=flaky)
    command = _command(clock, client_key="recover-binding")

    with pytest.raises(PersistenceUnavailableError):
        await service.submit(command=command)
    recovered = await service.submit(
        command=command.model_copy(update={"request_id": "recover-retry"})
    )

    assert flaky.calls == 2
    assert len(memory_state.tasks) == 1
    assert set(memory_state.tasks) == {recovered.task_view.task_id}
    assert len(memory_state.channel_bindings) == 1
    assert len(memory_state.projection_subscriptions) == 1

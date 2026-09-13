"""M7 渠道提交服务：权限、服务端幂等、绑定恢复与通知订阅。"""

from typing import Any

import pytest
from tests.conftest import drive_to_terminal

from xiaowei_agent.application.channel_access import (
    TaskAccessNotFoundError,
    TaskAccessService,
)
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
    TaskLookup,
    TaskStatus,
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
from xiaowei_agent.persistence.store import request_dedup_digest, submission_digest


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
    parent_task_id: str | None = None,
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
        parent_task_id=parent_task_id,
    )


def test_group_submission_requires_a_conversation_reference(clock) -> None:
    with pytest.raises(ValueError, match="group submission requires conversation_ref"):
        _command(clock).model_copy(update={"conversation_ref": None})


def test_only_web_submissions_can_name_a_parent(clock) -> None:
    with pytest.raises(ValueError, match="parent task is only supported for web"):
        _command(clock, parent_task_id="task-parent")


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
    class NeverMembership:
        async def is_current_group_member(self, **_: object) -> bool:
            raise AssertionError("web parent ownership must not query group membership")

    parent_access = TaskAccessService(
        runtime=runtime,
        task_store=store,
        channel_store=channel_store,
        membership=NeverMembership(),
    )
    return ChannelSubmissionService(
        runtime=runtime,
        channel_store=channel_store,
        web_parent_access=parent_access,
    )


async def _terminal_web_parent(
    service: ChannelSubmissionService,
    store: Any,
    clock: Any,
    *,
    principal: AuthenticatedPrincipal | None = None,
    client_key: str = "web-parent",
) -> Any:
    submitted = await service.submit(
        command=_command(
            clock,
            principal=principal,
            channel=ChannelKind.WEB,
            client_key=client_key,
        )
    )
    await drive_to_terminal(
        store,
        TaskLookup(
            task_id=submitted.task_view.task_id,
            tenant_id=(principal or _principal()).tenant_id,
            environment_id=(principal or _principal()).environment_id,
        ),
        TaskStatus.SUCCEEDED,
    )
    return submitted


async def test_web_parent_must_be_terminal_and_owned_by_the_same_web_identity(
    service, store, clock, memory_state
) -> None:
    parent = await _terminal_web_parent(service, store, clock)
    child = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="web-child",
            parent_task_id=parent.task_view.task_id,
        )
    )

    submission = await store.get_submission(
        lookup=TaskLookup(
            task_id=child.task_view.task_id,
            tenant_id="dev-local",
            environment_id="dev",
        )
    )

    assert submission.parent_task_id == parent.task_view.task_id
    assert child.binding.channel is ChannelKind.WEB
    assert len(memory_state.tasks) == 2


async def test_web_parent_fails_closed_when_authorizer_is_not_assembled(
    service, store, channel_store, memory_state, clock
) -> None:
    parent = await _terminal_web_parent(service, store, clock)
    unconfigured = ChannelSubmissionService(
        runtime=service._runtime,
        channel_store=channel_store,
    )
    existing_task_ids = set(memory_state.tasks)

    with pytest.raises(ChannelSubmissionForbiddenError, match="submission forbidden"):
        await unconfigured.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="missing-parent-authorizer",
                parent_task_id=parent.task_view.task_id,
            )
        )

    assert set(memory_state.tasks) == existing_task_ids


async def test_nonterminal_or_unknown_parent_is_hidden_without_creating_a_child(
    service, clock, memory_state
) -> None:
    pending = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="pending-parent",
        )
    )
    baseline = set(memory_state.tasks)

    for index, parent_task_id in enumerate(
        (pending.task_view.task_id, "missing-parent"), start=1
    ):
        with pytest.raises(TaskAccessNotFoundError, match="task not found"):
            await service.submit(
                command=_command(
                    clock,
                    channel=ChannelKind.WEB,
                    client_key=f"invalid-parent-child-{index}",
                    parent_task_id=parent_task_id,
                )
            )

    assert set(memory_state.tasks) == baseline


@pytest.mark.parametrize(
    "principal",
    (
        _principal(actor="bob"),
        _principal().model_copy(update={"subject_ref": "subject-other"}),
        _principal(tenant_id="other-tenant"),
        _principal(environment_id="prod"),
        _principal(
            actor="root",
            permissions=frozenset(
                {
                    ChannelPermission.VIEW_SAFE_TASK,
                    ChannelPermission.SUBMIT_READONLY_TASK,
                    ChannelPermission.ADMIN_ALL_SAFE_TASKS,
                }
            ),
        ),
    ),
    ids=("actor", "web-subject", "tenant", "environment", "admin"),
)
async def test_parent_authority_mismatch_is_hidden_even_from_admin(
    service, store, clock, memory_state, principal
) -> None:
    parent = await _terminal_web_parent(service, store, clock)
    baseline = set(memory_state.tasks)

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.submit(
            command=_command(
                clock,
                principal=principal,
                channel=ChannelKind.WEB,
                client_key=f"mismatched-parent-{principal.actor}",
                parent_task_id=parent.task_view.task_id,
            )
        )

    assert set(memory_state.tasks) == baseline


async def test_feishu_task_cannot_be_used_as_a_web_parent(
    service, store, clock, memory_state
) -> None:
    parent = await service.submit(command=_command(clock, client_key="feishu-parent"))
    await drive_to_terminal(
        store,
        TaskLookup(
            task_id=parent.task_view.task_id,
            tenant_id="dev-local",
            environment_id="dev",
        ),
        TaskStatus.SUCCEEDED,
    )
    baseline = set(memory_state.tasks)

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="web-child-of-feishu",
                parent_task_id=parent.task_view.task_id,
            )
        )

    assert set(memory_state.tasks) == baseline


async def test_corrupt_parent_cycle_is_hidden_without_creating_a_child(
    service, store, clock, memory_state
) -> None:
    parent = await _terminal_web_parent(service, store, clock)
    task_id = parent.task_view.task_id
    corrupt = memory_state.submissions[task_id].model_copy(
        update={"parent_task_id": task_id}
    )
    memory_state.submissions[task_id] = corrupt
    memory_state.submission_digests[task_id] = submission_digest(corrupt)
    memory_state.tasks[task_id] = memory_state.tasks[task_id].model_copy(
        update={
            "request_digest": request_dedup_digest(
                corrupt.envelope,
                corrupt.context,
                parent_task_id=task_id,
            )
        }
    )
    baseline = set(memory_state.tasks)

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="child-of-corrupt-cycle",
                parent_task_id=task_id,
            )
        )

    assert set(memory_state.tasks) == baseline


async def test_web_precheck_only_reads_the_direct_parent_and_worker_owns_ancestry(
    service, store, clock, memory_state
) -> None:
    root = await _terminal_web_parent(
        service,
        store,
        clock,
        client_key="ancestor-drift-root",
    )
    parent = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="ancestor-drift-parent",
            parent_task_id=root.task_view.task_id,
        )
    )
    await drive_to_terminal(
        store,
        TaskLookup(
            task_id=parent.task_view.task_id,
            tenant_id="dev-local",
            environment_id="dev",
        ),
        TaskStatus.SUCCEEDED,
    )
    root_id = root.task_view.task_id
    memory_state.tasks[root_id] = memory_state.tasks[root_id].model_copy(
        update={"status": TaskStatus.CREATED, "terminal_reason": None}
    )

    child = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="ancestor-drift-child",
            parent_task_id=parent.task_view.task_id,
        )
    )

    assert child.parent_task_id == parent.task_view.task_id
    assert len(memory_state.tasks) == 3


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
    service = ChannelSubmissionService(
        runtime=runtime,
        channel_store=flaky,
    )
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

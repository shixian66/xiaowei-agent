"""M7 渠道提交服务：权限、服务端幂等、绑定恢复与通知订阅。"""

from typing import Any

import pytest
from tests.conftest import drive_to_terminal

from xiaowei_agent.application.channel_access import (
    TaskAccessNotFoundError,
    TaskAccessService,
)
from xiaowei_agent.application.channel_submission import (
    ChannelParentNotFoundError,
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
    channel_idempotency_key,
    derive_channel_submission_references,
)
from xiaowei_agent.application.task_view_runtime import (
    SqlMessageNotAcceptedError,
    TaskViewRuntime,
)
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    DestinationKind,
    IdentitySource,
    ProjectionState,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.governance.sql_message import recognize_sql_message
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
    clarification_parent_task_id: str | None = None,
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
        private_chat_ref="p2p-chat-1" if channel is ChannelKind.FEISHU_PRIVATE else None,
        submitted_at=clock(),
        clarification_parent_task_id=clarification_parent_task_id,
    )


def test_group_submission_requires_a_conversation_reference(clock) -> None:
    with pytest.raises(ValueError, match="group submission requires conversation_ref"):
        _command(clock).model_copy(update={"conversation_ref": None})


def test_private_submission_requires_the_provider_p2p_chat(clock) -> None:
    with pytest.raises(ValueError, match="private submission requires private_chat_ref"):
        ChannelSubmitCommand.model_validate(
            _command(clock, channel=ChannelKind.FEISHU_PRIVATE).model_dump()
            | {"private_chat_ref": None}
        )


@pytest.mark.parametrize("channel", [ChannelKind.FEISHU_GROUP, ChannelKind.WEB])
def test_only_private_submissions_carry_a_p2p_chat(clock, channel: ChannelKind) -> None:
    with pytest.raises(ValueError, match="private_chat_ref is only supported for private"):
        ChannelSubmitCommand.model_validate(
            _command(clock, channel=channel).model_dump() | {"private_chat_ref": "p2p-chat-1"}
        )


def test_only_web_submissions_can_name_a_parent(clock) -> None:
    with pytest.raises(ValueError, match="parent task is only supported for web"):
        _command(clock, clarification_parent_task_id="task-parent")


@pytest.fixture
def channel_store(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


@pytest.fixture
def service(store, channel_store, memory_state):
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),
        recognize_sql=recognize_sql_message,
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
    terminal_status: TaskStatus = TaskStatus.CLARIFICATION_REQUIRED,
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
        terminal_status,
    )
    return submitted


async def test_web_clarification_parent_must_be_owned_by_the_same_web_identity(
    service, store, clock, memory_state
) -> None:
    parent = await _terminal_web_parent(service, store, clock)
    child = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="web-child",
            clarification_parent_task_id=parent.task_view.task_id,
        )
    )

    submission = await store.get_submission(
        lookup=TaskLookup(
            task_id=child.task_view.task_id,
            tenant_id="dev-local",
            environment_id="dev",
        )
    )

    assert submission.clarification_parent_task_id == parent.task_view.task_id
    assert child.binding.channel is ChannelKind.WEB
    assert len(memory_state.tasks) == 2


async def test_web_clarification_parent_is_consumed_at_most_once(
    service, store, clock, memory_state
) -> None:
    parent = await _terminal_web_parent(
        service,
        store,
        clock,
        client_key="clarification-parent-once",
        terminal_status=TaskStatus.CLARIFICATION_REQUIRED,
    )

    first = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="clarification-child-first",
            clarification_parent_task_id=parent.task_view.task_id,
        )
    )
    baseline = set(memory_state.tasks)

    with pytest.raises(ChannelParentNotFoundError, match="task not found"):
        await service.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="clarification-child-second",
                clarification_parent_task_id=parent.task_view.task_id,
            )
        )

    assert first.clarification_parent_task_id == parent.task_view.task_id
    assert set(memory_state.tasks) == baseline


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
                clarification_parent_task_id=parent.task_view.task_id,
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

    for index, clarification_parent_task_id in enumerate(
        (pending.task_view.task_id, "missing-parent"), start=1
    ):
        with pytest.raises(TaskAccessNotFoundError, match="task not found"):
            await service.submit(
                command=_command(
                    clock,
                    channel=ChannelKind.WEB,
                    client_key=f"invalid-parent-child-{index}",
                    clarification_parent_task_id=clarification_parent_task_id,
                )
            )

    assert set(memory_state.tasks) == baseline


async def test_non_clarification_terminal_parent_is_hidden_without_creating_a_child(
    service, store, clock, memory_state
) -> None:
    parent = await _terminal_web_parent(
        service,
        store,
        clock,
        client_key="succeeded-parent",
        terminal_status=TaskStatus.SUCCEEDED,
    )
    baseline = set(memory_state.tasks)

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="child-of-succeeded-parent",
                clarification_parent_task_id=parent.task_view.task_id,
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
                clarification_parent_task_id=parent.task_view.task_id,
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
        TaskStatus.CLARIFICATION_REQUIRED,
    )
    baseline = set(memory_state.tasks)

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="web-child-of-feishu",
                clarification_parent_task_id=parent.task_view.task_id,
            )
        )

    assert set(memory_state.tasks) == baseline


async def test_corrupt_parent_cycle_is_hidden_without_creating_a_child(
    service, store, clock, memory_state
) -> None:
    parent = await _terminal_web_parent(service, store, clock)
    task_id = parent.task_view.task_id
    corrupt = memory_state.submissions[task_id].model_copy(
        update={"clarification_parent_task_id": task_id}
    )
    memory_state.submissions[task_id] = corrupt
    memory_state.submission_digests[task_id] = submission_digest(corrupt)
    memory_state.tasks[task_id] = memory_state.tasks[task_id].model_copy(
        update={
            "request_digest": request_dedup_digest(
                corrupt.envelope,
                corrupt.context,
                clarification_parent_task_id=task_id,
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
                clarification_parent_task_id=task_id,
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
            clarification_parent_task_id=root.task_view.task_id,
        )
    )
    await drive_to_terminal(
        store,
        TaskLookup(
            task_id=parent.task_view.task_id,
            tenant_id="dev-local",
            environment_id="dev",
        ),
        TaskStatus.CLARIFICATION_REQUIRED,
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
            clarification_parent_task_id=parent.task_view.task_id,
        )
    )

    assert child.clarification_parent_task_id == parent.task_view.task_id
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


async def test_private_reply_goes_to_the_p2p_chat_not_the_user_id(
    service, channel_store, clock
) -> None:
    # 真实飞书对 receive_id_type=open_id 的私聊发送返回 230101；同一会话用
    # 事件里的 p2p chat_id 发送成功。私聊回复目标因此是会话，不是用户 id。
    submitted = await service.submit(
        command=_command(clock, channel=ChannelKind.FEISHU_PRIVATE)
    )
    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )

    assert [item.destination_ref for item in due] == ["p2p-chat-1"]
    assert submitted.binding.conversation_ref is None


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


async def _notices(channel_store) -> list:
    due = await channel_store.list_due_projection_subscriptions(
        query=ProjectionDueQuery(
            tenant_id="dev-local", environment_id="dev", limit=100
        )
    )
    return [
        item
        for item in due
        if item.destination_kind is DestinationKind.FEISHU_PRIVATE_NOTICE
    ]


async def test_web_notice_goes_to_the_users_latest_p2p_chat(
    service, channel_store, clock
) -> None:
    # 真实飞书拒绝按 open_id 发私聊（230101）；网页任务的通知改发到该用户最近一次
    # 私聊小维时记录的 p2p 会话。
    await service.submit(
        command=_command(clock, channel=ChannelKind.FEISHU_PRIVATE, client_key="p2p")
    )
    web = await service.submit(
        command=_command(clock, channel=ChannelKind.WEB, client_key="web-user")
    )

    notices = await _notices(channel_store)
    assert [(item.task_id, item.destination_ref) for item in notices] == [
        (web.task_view.task_id, "p2p-chat-1")
    ]


async def test_web_user_who_never_messaged_the_bot_gets_no_feishu_notice(
    service, channel_store, clock
) -> None:
    await service.submit(
        command=_command(clock, channel=ChannelKind.WEB, client_key="web-user")
    )
    other = _principal(actor="bob")
    await service.submit(
        command=_command(
            clock, principal=other, channel=ChannelKind.FEISHU_PRIVATE, client_key="bob"
        )
    )

    assert await _notices(channel_store) == []


async def test_web_user_gets_terminal_notice_but_admin_gets_no_subscription(
    service, channel_store, clock
) -> None:
    await service.submit(
        command=_command(clock, channel=ChannelKind.FEISHU_PRIVATE, client_key="p2p")
    )
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
    due = await _notices(channel_store)

    assert [item.task_id for item in due] == [ordinary.task_view.task_id]
    assert due[0].state is ProjectionState.WAITING_TERMINAL
    assert admin.task_view.task_id not in {item.task_id for item in due}


def test_server_idempotency_key_is_scoped_by_every_authority_dimension(clock) -> None:
    base = _command(clock)
    variants = (
        base.model_copy(update={"principal": _principal(actor="bob")}),
        base.model_copy(update={"principal": _principal(tenant_id="other-tenant")}),
        base.model_copy(update={"principal": _principal(environment_id="prod")}),
        base.model_copy(
            update={
                "channel": ChannelKind.FEISHU_PRIVATE,
                "conversation_ref": None,
                "private_chat_ref": "p2p-chat-1",
            }
        ),
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

        async def claim_source_event(self, *, command: Any) -> None:
            await self.delegate.claim_source_event(command=command)

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
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),
        recognize_sql=recognize_sql_message,
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


@pytest.mark.parametrize("failure", ["expired", "unavailable"])
async def test_sql_artifact_failures_pass_through_before_binding(
    store, channel_store, memory_state, clock, failure: str
) -> None:
    from xiaowei_agent.persistence.store import (
        SqlArtifactExpiredError,
        SqlArtifactUnavailableError,
        SqlArtifactUnavailableReason,
    )

    error: Exception = (
        SqlArtifactExpiredError()
        if failure == "expired"
        else SqlArtifactUnavailableError(reason=SqlArtifactUnavailableReason.NOT_FOUND)
    )

    class FailingRuntime:
        async def submit_clarification_child(self, **_: Any) -> Any:
            raise error

    class RecordingChannels:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate
            self.bind_calls = 0

        async def claim_source_event(self, *, command: Any) -> None:
            await self.delegate.claim_source_event(command=command)

        async def bind_task(self, *, command: Any) -> Any:
            self.bind_calls += 1
            return await self.delegate.bind_task(command=command)

        async def find_private_chat_ref(self, **kwargs: Any) -> Any:
            return await self.delegate.find_private_chat_ref(**kwargs)

    class AllowParent:
        async def require_web_parent_access(self, **_: Any) -> None:
            return None

    channels = RecordingChannels(channel_store)
    service = ChannelSubmissionService(
        runtime=FailingRuntime(),  # type: ignore[arg-type]
        channel_store=channels,  # type: ignore[arg-type]
        web_parent_access=AllowParent(),
    )
    command = _command(
        clock,
        channel=ChannelKind.WEB,
        client_key="sql-parent-answer",
        clarification_parent_task_id="task-parent",
    )

    with pytest.raises(type(error)) as caught:
        await service.submit(command=command)

    assert caught.value is error
    assert channels.bind_calls == 0
    assert memory_state.channel_bindings == {}


# --- F1：纯 SQL 消息分流提交（设计 §5.1） ----------------------------------------


@pytest.mark.parametrize("channel", [ChannelKind.WEB, ChannelKind.FEISHU_GROUP])
async def test_sql_message_creates_an_artifact_task_without_sql_in_channel_facts(
    service, channel_store, memory_state, clock, channel: ChannelKind
) -> None:
    import json

    from xiaowei_agent.contracts import ArtifactSubmission

    sql = "SELECT secret_marker_column FROM orders"
    command = _command(clock, channel=channel, client_key="sql-message", text=sql)

    first = await service.submit(command=command)
    replay = await service.submit(command=command.model_copy(update={"request_id": "r2"}))

    assert replay == first
    task_id = first.task_view.task_id
    assert set(memory_state.tasks) == {task_id}
    submission = memory_state.submissions[task_id]
    assert isinstance(submission, ArtifactSubmission)
    (artifact,) = memory_state.sql_artifacts.values()
    assert artifact.sql_bytes == sql.encode()
    assert artifact.sql_ref == submission.sql_ref
    assert first.binding.task_id == task_id
    channel_facts = json.dumps(
        [
            [item.model_dump(mode="json") for item in memory_state.channel_bindings.values()],
            [
                item.model_dump(mode="json")
                for item in memory_state.projection_subscriptions.values()
            ],
        ],
        ensure_ascii=False,
    )
    assert "secret_marker_column" not in channel_facts
    assert "secret_marker_column" not in submission.model_dump_json()


async def test_same_key_with_different_sql_is_an_idempotency_conflict(
    service, clock
) -> None:
    await service.submit(command=_command(clock, client_key="sql-conflict", text="SELECT 1"))
    with pytest.raises(IdempotencyConflictError):
        await service.submit(
            command=_command(clock, client_key="sql-conflict", text="SELECT 2")
        )


@pytest.mark.parametrize(
    "text",
    ["SELECT 'password=hunter2", "SELECT TRUE AND TRUE", "show data for yesterday"],
)
@pytest.mark.parametrize("channel", [ChannelKind.WEB, ChannelKind.FEISHU_PRIVATE])
async def test_sql_like_text_is_rejected_before_any_channel_or_task_fact(
    service, memory_state, clock, text: str, channel: ChannelKind
) -> None:
    """像 SQL 的原文不能落进普通对话提交（没有 24 小时清理），也不占用来源事件。"""
    from xiaowei_agent.application.task_view_runtime import SqlLikeTextNotAcceptedError

    with pytest.raises(SqlLikeTextNotAcceptedError):
        await service.submit(
            command=_command(clock, channel=channel, client_key="sql-like", text=text)
        )

    assert memory_state.tasks == {}
    assert memory_state.submissions == {}
    assert memory_state.sql_artifacts == {}
    assert memory_state.channel_source_kinds == {}
    assert memory_state.channel_bindings == {}


async def test_conversation_text_keeps_the_8192_character_limit(service, clock) -> None:
    from xiaowei_agent.application.channel_submission import ChannelMessageTooLongError

    await service.submit(command=_command(clock, client_key="long-ok", text="慢" * 8192))
    with pytest.raises(ChannelMessageTooLongError):
        await service.submit(
            command=_command(clock, client_key="long-no", text="慢" * 8193)
        )


async def test_sql_text_is_bounded_by_65536_utf8_bytes(service, memory_state, clock) -> None:
    from pydantic import ValidationError

    from xiaowei_agent.application.channel_submission import ChannelMessageTooLongError

    prefix = "SELECT '"
    filler = "x" * (65_536 - len(prefix) - 1)
    at_limit = f"{prefix}{filler}'"
    assert len(at_limit.encode()) == 65_536
    await service.submit(command=_command(clock, client_key="sql-max", text=at_limit))
    assert len(memory_state.sql_artifacts) == 1
    # 渠道文本超过 64 KiB 在命令校验时即拒绝。
    for over in (f"{prefix}{filler}x'", "中" * 21_846):
        with pytest.raises(ValidationError):
            _command(clock, client_key="bytes-over", text=over)
    # 64 KiB 以内、但不是 SQL 的长文本仍按普通消息上限拒绝。
    with pytest.raises(ChannelMessageTooLongError):
        await service.submit(
            command=_command(clock, client_key="not-sql", text="SELECT 一下" + "x" * 9000)
        )
    assert len(memory_state.sql_artifacts) == 1


async def test_sql_shaped_clarification_answer_is_rejected_without_consuming_the_parent(
    store, channel_store, memory_state, clock, context
) -> None:
    from tests.conftest import make_envelope, make_submission

    parent = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="sql-answer-parent", idempotency_key="sql-answer-parent"
            ),
        )
    )
    await drive_to_terminal(
        store,
        TaskLookup(task_id=parent.task_id, tenant_id="dev-local", environment_id="dev"),
        TaskStatus.CLARIFICATION_REQUIRED,
    )

    class AllowParent:
        async def require_web_parent_access(self, **_: Any) -> None:
            return None

    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),
        recognize_sql=recognize_sql_message,
    )
    service = ChannelSubmissionService(
        runtime=runtime, channel_store=channel_store, web_parent_access=AllowParent()
    )
    # 澄清回答永远不当作新的 SQL；是 SQL 形状的回答也不能变成普通对话子任务进入模型。
    tasks_before = set(memory_state.tasks)
    with pytest.raises(SqlMessageNotAcceptedError):
        await service.submit(
            command=_command(
                clock,
                channel=ChannelKind.WEB,
                client_key="sql-answer",
                text="SELECT 1",
                clarification_parent_task_id=parent.task_id,
            )
        )
    assert set(memory_state.tasks) == tasks_before
    assert memory_state.sql_artifacts == {}
    assert memory_state.channel_bindings == {}

    # 父任务未被消费：普通回答仍能作答。
    submitted = await service.submit(
        command=_command(
            clock,
            channel=ChannelKind.WEB,
            client_key="plain-answer",
            text="最近30分钟",
            clarification_parent_task_id=parent.task_id,
        )
    )
    child = memory_state.submissions[submitted.task_view.task_id]
    assert child.clarification_parent_task_id == parent.task_id


# --- F1：同一渠道事件只能选出一种提交类型（审查 P1-2） ---------------------------
#
# TaskStore 按 ADR-018 D2 让对话与 SQL 的幂等键分属不同作用域；渠道来源引用却只有一个。
# 渠道边界必须在创建任务之前原子地为来源事件选定提交类型，否则第二种类型会留下一个
# 已提交、未绑定、可被 worker 领取的任务。

_SQL_TEXT = "SELECT id FROM orders"


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [("inspect slow queries", _SQL_TEXT), (_SQL_TEXT, "inspect slow queries")],
    ids=["conversation-then-sql", "sql-then-conversation"],
)
async def test_one_channel_event_never_leaves_a_second_task_of_the_other_kind(
    service, clock, memory_state, first_text: str, second_text: str
) -> None:
    first = await service.submit(command=_command(clock, text=first_text))
    artifacts_after_first = dict(memory_state.sql_artifacts)

    with pytest.raises(IdempotencyConflictError):
        await service.submit(command=_command(clock, text=second_text))

    assert set(memory_state.tasks) == {first.task_view.task_id}
    assert memory_state.sql_artifacts == artifacts_after_first
    assert [b.task_id for b in memory_state.channel_bindings.values()] == [
        first.task_view.task_id
    ]


@pytest.mark.parametrize("text", ["inspect slow queries", _SQL_TEXT])
async def test_same_kind_replay_of_one_channel_event_keeps_one_task(
    service, clock, memory_state, text: str
) -> None:
    first = await service.submit(command=_command(clock, text=text))
    replay = await service.submit(command=_command(clock, text=text))

    assert replay.task_view.task_id == first.task_view.task_id
    assert len(memory_state.tasks) == 1
    assert len(memory_state.channel_bindings) == 1

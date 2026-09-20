"""M7 任务访问服务：统一 not_found、群成员实时校验与安全投影。"""

import logging
from typing import Any

import pytest
from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

from xiaowei_agent.application import channel_access as channel_access_module
from xiaowei_agent.application.channel_access import (
    AccessibleTask,
    TaskAccessNotFoundError,
    TaskAccessQuery,
    TaskAccessService,
    TaskAccessSnapshotUnavailableError,
    TaskListQuery,
)
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    IdentitySource,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.persistence.channel import BindTaskCommand
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryChannelStore
from xiaowei_agent.persistence.plans import InMemoryPlanStore
from xiaowei_agent.persistence.store import TransitionCommand


class MembershipStub:
    def __init__(self, *, result: bool = False, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        self.calls.append((tenant_id, conversation_ref, subject_ref))
        if self.error is not None:
            raise self.error
        return self.result


def _principal(
    *,
    actor: str = "alice",
    subject_ref: str = "subject-alice",
    tenant_id: str = "dev-local",
    environment_id: str = "dev",
    permissions: frozenset[ChannelPermission] | None = None,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id=tenant_id,
        environment_id=environment_id,
        actor=actor,
        source=IdentitySource.FEISHU,
        subject_ref=subject_ref,
        permissions=permissions
        if permissions is not None
        else frozenset({ChannelPermission.VIEW_SAFE_TASK}),
    )


def _service(store: Any, channel_store: Any, memory_state: Any, membership: Any):
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
        snapshot=StaticCapabilityRegistry().snapshot(),
    )
    return TaskAccessService(
        runtime=runtime,
        task_store=store,
        channel_store=channel_store,
        membership=membership,
    )


async def _create_task(
    store: Any,
    context: Any,
    *,
    suffix: str,
    actor: str = "alice",
):
    scoped = context.model_copy(update={"actor": actor})
    return await store.create_task(
        submission=make_submission(
            scoped,
            envelope=make_envelope(
                request_id=f"request-{suffix}",
                actor=actor,
                idempotency_key=f"access-{suffix}",
                text=f"inspect {suffix}",
            ),
        )
    )


@pytest.fixture
def channel_store(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


async def test_owner_and_admin_can_read_without_a_membership_call(
    store, channel_store, memory_state, context
) -> None:
    task = await _create_task(store, context, suffix="owner-admin")
    membership = MembershipStub(error=RuntimeError("must not be called"))
    service = _service(store, channel_store, memory_state, membership)

    owner = await service.get_task(
        query=TaskAccessQuery(principal=_principal(), task_id=task.task_id)
    )
    admin = await service.get_task(
        query=TaskAccessQuery(
            principal=_principal(
                actor="root",
                subject_ref="subject-root",
                permissions=frozenset(
                    {
                        ChannelPermission.VIEW_SAFE_TASK,
                        ChannelPermission.ADMIN_ALL_SAFE_TASKS,
                    }
                ),
            ),
            task_id=task.task_id,
        )
    )

    assert owner.task_view.task_id == task.task_id
    assert admin.task_view == owner.task_view
    assert membership.calls == []


async def test_task_detail_returns_only_the_parent_reference(
    store, channel_store, memory_state, context
) -> None:
    parent = await _create_task(store, context, suffix="parent-reference-parent")
    await drive_to_terminal(
        store,
        lookup_for(parent),
        TaskStatus.CLARIFICATION_REQUIRED,
    )
    task = await store.create_clarification_child(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="request-parent-reference-child",
                idempotency_key="access-parent-reference-child",
                text="inspect parent reference child",
            ),
            clarification_parent_task_id=parent.task_id,
        ),
        authenticated_channel_owner=context.actor,
    )
    service = _service(store, channel_store, memory_state, MembershipStub())

    accessible = await service.get_task(
        query=TaskAccessQuery(principal=_principal(), task_id=task.task_id)
    )

    assert accessible.clarification_parent_task_id == parent.task_id
    assert not {"submission", "binding", "parent_submission"} & set(
        AccessibleTask.model_fields
    )


async def test_current_group_member_can_read_and_is_checked_on_every_request(
    store, channel_store, memory_state, context, clock
) -> None:
    task = await _create_task(store, context, suffix="group-member")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref="event-group-member",
            created_at=clock(),
        )
    )
    membership = MembershipStub(result=True)
    service = _service(store, channel_store, memory_state, membership)
    query = TaskAccessQuery(
        principal=_principal(actor="bob", subject_ref="subject-bob"),
        task_id=task.task_id,
    )

    await service.get_task(query=query)
    await service.get_task(query=query)

    assert membership.calls == [
        ("dev-local", "chat-1", "subject-bob"),
        ("dev-local", "chat-1", "subject-bob"),
    ]


async def test_absent_membership_port_fails_closed_for_group_scoped_reads(
    store, channel_store, memory_state, context, clock
) -> None:
    """飞书未装配时 `membership=None`，群成员校验无从进行，必须拒绝。

    RI5 让飞书变成可选插件，于是 `membership` 可能整个不存在。这条守的是
    **降级方向**：不能因为"没法校验"就放行——那会让一个没装配飞书的部署
    把群任务开放给任何登录用户。
    """
    task = await _create_task(store, context, suffix="absent-membership")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref="event-absent-membership",
            created_at=clock(),
        )
    )
    query = TaskAccessQuery(
        principal=_principal(actor="bob", subject_ref="subject-bob"),
        task_id=task.task_id,
    )

    absent = _service(store, channel_store, memory_state, None)
    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await absent.get_task(query=query)

    # 对照：装配了 membership 且确认在群里时，同一次读取是放行的——
    # 说明上面的拒绝来自"没法校验"，不是这条路径本来就读不到。
    present = _service(store, channel_store, memory_state, MembershipStub(result=True))
    assert await present.get_task(query=query)


async def test_absent_membership_port_does_not_block_the_owner_or_admin(
    store, channel_store, memory_state, context, clock
) -> None:
    """该 fail-closed 不得波及 RI5 闭环：本人任务与 admin 全量安全任务仍可读。"""
    task = await _create_task(store, context, suffix="absent-membership-owner")
    service = _service(store, channel_store, memory_state, None)

    owner = TaskAccessQuery(
        principal=_principal(actor="alice", subject_ref="subject-alice"),
        task_id=task.task_id,
    )
    admin = TaskAccessQuery(
        principal=_principal(
            actor="admin",
            subject_ref="subject-admin",
            permissions=frozenset(
                {
                    ChannelPermission.VIEW_SAFE_TASK,
                    ChannelPermission.ADMIN_ALL_SAFE_TASKS,
                }
            ),
        ),
        task_id=task.task_id,
    )

    assert await service.get_task(query=owner)
    assert await service.get_task(query=admin)


@pytest.mark.parametrize(
    "membership",
    [MembershipStub(result=False), MembershipStub(error=TimeoutError())],
)
async def test_non_member_and_membership_failure_share_the_same_not_found(
    store, channel_store, memory_state, context, clock, membership
) -> None:
    task = await _create_task(store, context, suffix=f"denied-{id(membership)}")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref=f"event-{task.task_id}",
            created_at=clock(),
        )
    )
    service = _service(store, channel_store, memory_state, membership)

    with pytest.raises(TaskAccessNotFoundError, match="task not found") as denied:
        await service.get_task(
            query=TaskAccessQuery(
                principal=_principal(actor="mallory", subject_ref="subject-mallory"),
                task_id=task.task_id,
            )
        )
    assert task.task_id not in str(denied.value)


async def test_membership_failure_emits_only_a_safe_structured_signal(
    store, channel_store, memory_state, context, clock, caplog
) -> None:
    task = await _create_task(store, context, suffix="membership-observability")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-sensitive-ref",
            source_event_ref="event-membership-observability",
            created_at=clock(),
        )
    )
    raw_error = "provider token=" + "hunter" + "2-plain"
    service = _service(
        store,
        channel_store,
        memory_state,
        MembershipStub(error=RuntimeError(raw_error)),
    )

    with caplog.at_level(
        logging.WARNING, logger="xiaowei_agent.application.channel_access"
    ):
        with pytest.raises(TaskAccessNotFoundError):
            await service.get_task(
                query=TaskAccessQuery(
                    principal=_principal(
                        actor="bob", subject_ref="subject-sensitive-ref"
                    ),
                    task_id=task.task_id,
                )
            )

    records = [
        record
        for record in caplog.records
        if record.getMessage() == "feishu membership check failed"
    ]
    assert len(records) == 1
    assert records[0].tenant_id == "dev-local"
    assert records[0].environment_id == "dev"
    assert records[0].failure_kind == "membership_adapter_error"
    serialized = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in records
    )
    for forbidden in (
        raw_error,
        task.task_id,
        "chat-sensitive-ref",
        "subject-sensitive-ref",
    ):
        assert forbidden not in serialized


async def test_non_member_does_not_emit_a_provider_failure_signal(
    store, channel_store, memory_state, context, clock, caplog
) -> None:
    task = await _create_task(store, context, suffix="membership-denied-signal")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref="event-membership-denied-signal",
            created_at=clock(),
        )
    )
    service = _service(
        store, channel_store, memory_state, MembershipStub(result=False)
    )

    with caplog.at_level(
        logging.WARNING, logger="xiaowei_agent.application.channel_access"
    ):
        with pytest.raises(TaskAccessNotFoundError):
            await service.get_task(
                query=TaskAccessQuery(
                    principal=_principal(actor="bob", subject_ref="subject-bob"),
                    task_id=task.task_id,
                )
            )

    assert not [
        record
        for record in caplog.records
        if record.getMessage() == "feishu membership check failed"
    ]


async def test_membership_diagnostic_failure_preserves_hidden_not_found(
    store, channel_store, memory_state, context, clock, monkeypatch
) -> None:
    task = await _create_task(store, context, suffix="membership-log-failure")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref="event-membership-log-failure",
            created_at=clock(),
        )
    )

    def fail_logging(*args, **kwargs):
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(channel_access_module._LOGGER, "warning", fail_logging)
    service = _service(
        store,
        channel_store,
        memory_state,
        MembershipStub(error=TimeoutError("membership unavailable")),
    )

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.get_task(
            query=TaskAccessQuery(
                principal=_principal(actor="bob", subject_ref="subject-bob"),
                task_id=task.task_id,
            )
        )


async def test_cross_scope_missing_permission_and_private_non_owner_are_indistinguishable(
    store, channel_store, memory_state, context
) -> None:
    task = await _create_task(store, context, suffix="hidden")
    service = _service(store, channel_store, memory_state, MembershipStub(result=True))
    principals = (
        _principal(tenant_id="other-tenant"),
        _principal(actor="mallory", subject_ref="subject-mallory"),
        _principal(permissions=frozenset()),
    )

    messages = []
    for principal in principals:
        with pytest.raises(TaskAccessNotFoundError) as hidden:
            await service.get_task(
                query=TaskAccessQuery(principal=principal, task_id=task.task_id)
            )
        messages.append(str(hidden.value))
    assert messages == ["task not found"] * len(principals)


async def test_task_list_requires_view_permission(
    store, channel_store, memory_state
) -> None:
    service = _service(store, channel_store, memory_state, MembershipStub())

    with pytest.raises(TaskAccessNotFoundError, match="task not found"):
        await service.list_tasks(
            query=TaskListQuery(
                principal=_principal(permissions=frozenset()),
                limit=10,
            )
        )


async def test_accessible_task_redacts_request_text_before_returning_it(
    store, channel_store, memory_state, context
) -> None:
    raw = "inspect password=" + "hunter" + "2-plain"
    task = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(idempotency_key="access-redaction", text=raw),
        )
    )
    service = _service(store, channel_store, memory_state, MembershipStub())

    result = await service.get_task(
        query=TaskAccessQuery(principal=_principal(), task_id=task.task_id)
    )

    assert result.request_preview == "inspect password=***"
    assert raw not in result.model_dump_json()


async def test_detail_reuses_the_authorized_record_for_projection(
    store, channel_store, memory_state, context
) -> None:
    task = await _create_task(store, context, suffix="read-count")

    class CountingTaskReads:
        def __init__(self) -> None:
            self.record_reads = 0
            self.submission_reads = 0

        async def get(self, *, lookup):
            self.record_reads += 1
            return await store.get(lookup=lookup)

        async def get_submission(self, *, lookup):
            self.submission_reads += 1
            return await store.get_submission(lookup=lookup)

    counting = CountingTaskReads()
    runtime = TaskViewRuntime(
        task_store=counting,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
        snapshot=StaticCapabilityRegistry().snapshot(),
    )
    service = TaskAccessService(
        runtime=runtime,
        task_store=counting,
        channel_store=channel_store,
        membership=MembershipStub(),
    )

    detail = await service.get_task(
        query=TaskAccessQuery(principal=_principal(), task_id=task.task_id)
    )

    assert detail.task_view.task_id == task.task_id
    assert counting.record_reads == 2
    assert counting.submission_reads == 1


async def test_detail_retries_when_status_changes_during_runtime_projection(
    store, channel_store, memory_state, context
) -> None:
    task = await _create_task(store, context, suffix="snapshot-race")
    real_runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
        snapshot=StaticCapabilityRegistry().snapshot(),
    )

    class AdvanceOnceRuntime:
        def __init__(self) -> None:
            self.calls = 0

        async def project_task(self, *, record):
            view = await real_runtime.project_task(record=record)
            self.calls += 1
            if self.calls == 1:
                current = await store.get(
                    lookup=TaskLookup(
                        task_id=record.task_id,
                        tenant_id=record.tenant_id,
                        environment_id=record.environment_id,
                    )
                )
                await store.transition(
                    command=TransitionCommand(
                        task_id=task.task_id,
                        expected_version=current.version,
                        to_status=TaskStatus.PLANNING,
                    )
                )
            return view

    runtime = AdvanceOnceRuntime()
    service = TaskAccessService(
        runtime=runtime,
        task_store=store,
        channel_store=channel_store,
        membership=MembershipStub(),
    )

    detail = await service.get_task(
        query=TaskAccessQuery(principal=_principal(), task_id=task.task_id)
    )

    assert runtime.calls == 2
    assert detail.task_view.status is TaskStatus.PLANNING
    assert detail.task_version == 1


async def test_detail_fails_closed_when_no_consistent_snapshot_can_be_formed(
    store, channel_store, memory_state, context
) -> None:
    task = await _create_task(store, context, suffix="snapshot-unavailable")
    real_runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
        snapshot=StaticCapabilityRegistry().snapshot(),
    )

    class ContinuouslyChangingReads:
        def __init__(self) -> None:
            self.reads = 0

        async def get(self, *, lookup):
            self.reads += 1
            record = await store.get(lookup=lookup)
            return record.model_copy(update={"version": record.version + self.reads})

        async def get_submission(self, *, lookup):
            return await store.get_submission(lookup=lookup)

    changing = ContinuouslyChangingReads()
    service = TaskAccessService(
        runtime=real_runtime,
        task_store=changing,
        channel_store=channel_store,
        membership=MembershipStub(),
    )

    with pytest.raises(
        TaskAccessSnapshotUnavailableError, match="task snapshot unavailable"
    ):
        await service.get_task(
            query=TaskAccessQuery(principal=_principal(), task_id=task.task_id)
        )

    assert changing.reads == 4  # 一次授权读取 + 每轮一次 winner 复核。


async def test_user_list_excludes_group_tasks_while_admin_list_keeps_the_scope(
    store, channel_store, memory_state, context, clock
) -> None:
    private = await _create_task(store, context, suffix="private")
    group = await _create_task(store, context, suffix="group")
    other_actor = await _create_task(store, context, suffix="other-actor", actor="bob")
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=group.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref="event-list-group",
            created_at=clock(),
        )
    )
    service = _service(store, channel_store, memory_state, MembershipStub())

    mine = await service.list_tasks(
        query=TaskListQuery(principal=_principal(), limit=100)
    )
    admin = await service.list_tasks(
        query=TaskListQuery(
            principal=_principal(
                actor="root",
                subject_ref="subject-root",
                permissions=frozenset(
                    {
                        ChannelPermission.VIEW_SAFE_TASK,
                        ChannelPermission.ADMIN_ALL_SAFE_TASKS,
                    }
                ),
            ),
            limit=100,
        )
    )

    assert [item.task_id for item in mine.items] == [private.task_id]
    assert {item.task_id for item in admin.items} == {
        private.task_id,
        group.task_id,
        other_actor.task_id,
    }


async def test_user_list_uses_one_scoped_group_binding_batch(
    store, memory_state, context, clock
) -> None:
    class CountingChannelStore(InMemoryChannelStore):
        def __init__(self) -> None:
            super().__init__(clock=clock, state=memory_state)
            self.single_group_reads = 0
            self.batch_group_reads = 0

        async def get_group_binding(self, *, lookup):
            self.single_group_reads += 1
            return await super().get_group_binding(lookup=lookup)

        async def list_group_bound_task_ids(self, *, query):
            self.batch_group_reads += 1
            return await super().list_group_bound_task_ids(query=query)

    channel_store = CountingChannelStore()
    for index in range(10):
        await _create_task(store, context, suffix=f"batch-list-{index}")
    service = _service(store, channel_store, memory_state, MembershipStub())

    page = await service.list_tasks(
        query=TaskListQuery(principal=_principal(), limit=10)
    )

    assert len(page.items) == 10
    assert channel_store.batch_group_reads == 1
    assert channel_store.single_group_reads == 0


async def test_group_only_scan_page_keeps_a_cursor_to_older_private_tasks(
    store, channel_store, memory_state, context, clock
) -> None:
    private = await _create_task(store, context, suffix="old-private")
    for index in range(100):
        group = await _create_task(store, context, suffix=f"new-group-{index}")
        await channel_store.bind_task(
            command=BindTaskCommand(
                task_id=group.task_id,
                tenant_id="dev-local",
                environment_id="dev",
                channel=ChannelKind.FEISHU_GROUP,
                initiator_subject_ref="subject-alice",
                conversation_ref="chat-1",
                source_event_ref=f"event-group-page-{index}",
                created_at=clock(),
            )
        )
    service = _service(store, channel_store, memory_state, MembershipStub())

    group_only_page = await service.list_tasks(
        query=TaskListQuery(principal=_principal(), limit=1)
    )
    assert group_only_page.items == ()
    assert group_only_page.next_created_seq is not None

    next_page = await service.list_tasks(
        query=TaskListQuery(
            principal=_principal(),
            before_created_seq=group_only_page.next_created_seq,
            limit=1,
        )
    )
    assert [item.task_id for item in next_page.items] == [private.task_id]
    assert next_page.next_created_seq is None

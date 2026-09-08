"""M7 渠道身份、分页与投影输入的闭集契约。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    ActorTaskPageQuery,
    AuthenticatedPrincipal,
    Channel,
    ChannelKind,
    ChannelPermission,
    DestinationKind,
    FeishuProjectionInput,
    IdentitySource,
    ProjectionErrorCode,
    ProjectionState,
    RequestContext,
    RequestEnvelope,
    ScopeTaskPageQuery,
    StoredTaskPage,
    StoredTaskRead,
    TaskRecord,
    TaskStatus,
    TaskSubmission,
    TaskView,
    task_query_path,
)


def _principal(**updates: object) -> AuthenticatedPrincipal:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "environment_id": "dev",
        "actor": "alice",
        "source": IdentitySource.FEISHU,
        "subject_ref": "ou-subject-1",
        "permissions": frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    }
    return AuthenticatedPrincipal(**(values | updates))


def _view() -> TaskView:
    return TaskView(
        task_id="task-1",
        status=TaskStatus.CREATED,
        render=None,
        query_path=task_query_path("task-1"),
    )


def _stored_task(*, task_id: str, created_seq: int) -> StoredTaskRead:
    return StoredTaskRead(
        record=TaskRecord(
            task_id=task_id,
            tenant_id="tenant-a",
            environment_id="dev",
            actor="alice",
            idempotency_key=f"idem-{task_id}",
            request_digest="a" * 64,
            status=TaskStatus.CREATED,
            version=0,
            created_seq=created_seq,
            attempt_number=0,
            task_failure_count=0,
            next_attempt_at=None,
        ),
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id=f"request-{task_id}",
                tenant_id="tenant-a",
                actor="alice",
                channel=Channel.FEISHU,
                text="检查慢查询",
                idempotency_key=f"idem-{task_id}",
                environment_id="dev",
            ),
            context=RequestContext(
                tenant_id="tenant-a",
                actor="alice",
                environment_id="dev",
                trace_id="0" * 32,
                policy_revision="policy-1",
            ),
            as_of=dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC),
        ),
    )


def test_channel_enums_are_exact_closed_sets() -> None:
    assert {item.value for item in ChannelPermission} == {
        "view_safe_task",
        "submit_readonly_task",
        "admin_all_safe_tasks",
    }
    assert {item.value for item in IdentitySource} == {"feishu"}
    assert {item.value for item in ChannelKind} == {
        "feishu_private",
        "feishu_group",
        "web",
    }
    assert {item.value for item in DestinationKind} == {
        "feishu_message_card",
        "feishu_private_notice",
    }
    assert {item.value for item in ProjectionState} == {
        "pending_initial",
        "waiting_terminal",
        "delivering_terminal",
        "completed",
        "dead_letter",
    }
    assert {item.value for item in ProjectionErrorCode} == {
        "provider_rate_limited",
        "provider_timeout",
        "provider_unavailable",
        "provider_unauthorized",
        "provider_forbidden",
        "provider_invalid_payload",
        "provider_internal",
    }


@pytest.mark.parametrize(
    ("enum_type", "unknown"),
    [
        (ChannelPermission, "approve_task"),
        (IdentitySource, "password"),
        (ChannelKind, "webhook"),
        (DestinationKind, "database_export"),
        (ProjectionState, "succeeded"),
        (ProjectionErrorCode, "raw_provider_error"),
    ],
)
def test_channel_enums_reject_unknown_values(enum_type: type[object], unknown: str) -> None:
    with pytest.raises(ValueError):
        enum_type(unknown)


def test_authenticated_principal_has_only_server_identity_and_three_permissions() -> None:
    principal = _principal()

    assert set(AuthenticatedPrincipal.model_fields) == {
        "tenant_id",
        "environment_id",
        "actor",
        "source",
        "subject_ref",
        "permissions",
    }
    assert principal.permissions == frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"actor": ""},
        {"actor": " alice "},
        {"source": "feishu"},
        {"permissions": {ChannelPermission.VIEW_SAFE_TASK}},
        {"permissions": frozenset({"approve_task"})},
        {"role": "admin"},
    ],
)
def test_authenticated_principal_rejects_lax_or_unknown_fields(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _principal(**updates)


def test_task_page_queries_have_separate_actor_and_admin_scope_shapes() -> None:
    actor_query = ActorTaskPageQuery(
        tenant_id="tenant-a",
        environment_id="dev",
        actor="alice",
        after_created_seq=None,
        limit=100,
    )
    scope_query = ScopeTaskPageQuery(
        tenant_id="tenant-a",
        environment_id="dev",
        after_created_seq=42,
        limit=1,
    )

    assert set(ActorTaskPageQuery.model_fields) == {
        "tenant_id",
        "environment_id",
        "actor",
        "after_created_seq",
        "limit",
    }
    assert set(ScopeTaskPageQuery.model_fields) == {
        "tenant_id",
        "environment_id",
        "after_created_seq",
        "limit",
    }
    assert actor_query.limit == 100
    assert scope_query.after_created_seq == 42
    assert set(StoredTaskRead.model_fields) == {"record", "submission"}
    assert set(StoredTaskPage.model_fields) == {"items", "next_created_seq"}


@pytest.mark.parametrize(
    "query",
    [
        lambda: ActorTaskPageQuery(
            tenant_id="tenant-a", environment_id="dev", actor="alice", limit=0
        ),
        lambda: ActorTaskPageQuery(
            tenant_id="tenant-a", environment_id="dev", actor="alice", limit=101
        ),
        lambda: ScopeTaskPageQuery(
            tenant_id="tenant-a", environment_id="dev", after_created_seq=0, limit=10
        ),
        lambda: ScopeTaskPageQuery(
            tenant_id="tenant-a", environment_id="dev", limit=True
        ),
    ],
)
def test_task_page_queries_reject_invalid_bounds(query: object) -> None:
    with pytest.raises(ValidationError):
        query()


@pytest.mark.parametrize("cursor", [0, -1, True])
def test_stored_task_page_rejects_a_cursor_the_next_query_cannot_consume(
    cursor: object,
) -> None:
    with pytest.raises(ValidationError):
        StoredTaskPage(items=(), next_created_seq=cursor)


def test_empty_task_page_cannot_claim_a_continuation_cursor() -> None:
    assert StoredTaskPage(items=(), next_created_seq=None).items == ()
    with pytest.raises(ValidationError, match="empty page cannot have a cursor"):
        StoredTaskPage(items=(), next_created_seq=42)


def test_task_page_cursor_must_identify_the_last_returned_record() -> None:
    items = (
        _stored_task(task_id="task-2", created_seq=42),
        _stored_task(task_id="task-1", created_seq=41),
    )

    with pytest.raises(ValidationError, match="cursor must match the last item"):
        StoredTaskPage(items=items, next_created_seq=42)

    page = StoredTaskPage(items=items, next_created_seq=41)
    assert page.next_created_seq == 41
    assert StoredTaskPage.model_validate_json(page.model_dump_json()) == page
    assert StoredTaskPage(items=items, next_created_seq=None).next_created_seq is None


@pytest.mark.parametrize("created_seqs", [(41, 42), (42, 42)])
def test_task_page_items_must_be_strictly_descending(
    created_seqs: tuple[int, int],
) -> None:
    items = tuple(
        _stored_task(task_id=f"task-{index}", created_seq=created_seq)
        for index, created_seq in enumerate(created_seqs)
    )

    with pytest.raises(ValidationError, match="strictly descending"):
        StoredTaskPage(items=items, next_created_seq=created_seqs[-1])


def test_feishu_projection_input_is_only_safe_task_view_preview_version_and_url() -> None:
    projection = FeishuProjectionInput(
        task_view=_view(),
        request_preview="检查最近三十分钟慢查询",
        task_version=0,
        detail_url="https://xiaowei.example.test/app/tasks/task-1",
    )

    assert set(FeishuProjectionInput.model_fields) == {
        "task_view",
        "request_preview",
        "task_version",
        "detail_url",
    }
    assert projection.task_view == _view()
    assert projection.task_version == 0
    assert FeishuProjectionInput.model_validate_json(projection.model_dump_json()) == projection


def _projection_values() -> dict[str, object]:
    return {
        "task_view": _view(),
        "task_version": 0,
        "detail_url": "https://xiaowei.example.test/app/tasks/task-1",
    }


def test_request_preview_preserves_text_whitespace() -> None:
    padded = " 检查最近三十分钟慢查询\n"

    projection = FeishuProjectionInput(
        **(_projection_values() | {"request_preview": padded})
    )

    assert projection.request_preview == padded


def test_request_preview_rejects_empty_or_oversized_text() -> None:
    values = _projection_values()

    assert len(
        FeishuProjectionInput(
            **(values | {"request_preview": "x" * 8192})
        ).request_preview
    ) == 8192
    with pytest.raises(ValidationError):
        FeishuProjectionInput(**(values | {"request_preview": ""}))
    with pytest.raises(ValidationError):
        FeishuProjectionInput(**(values | {"request_preview": "x" * 8193}))


@pytest.mark.parametrize(
    "updates",
    [
        {"task_version": -1},
        {"task_version": True},
        {"detail_url": "http://xiaowei.example.test/app/tasks/task-1"},
        {"detail_url": "file:///tmp/result"},
        {"raw_rows": ({"secret": "value"},)},
        {"evidence": ({"fact": "raw"},)},
    ],
)
def test_feishu_projection_input_rejects_invalid_or_extra_fields(
    updates: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "task_view": _view(),
        "request_preview": "检查最近三十分钟慢查询",
        "task_version": 0,
        "detail_url": "https://xiaowei.example.test/app/tasks/task-1",
    }
    with pytest.raises(ValidationError):
        FeishuProjectionInput(**(values | updates))

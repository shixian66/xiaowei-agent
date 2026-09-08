"""M7 渠道身份、分页与投影输入的闭集契约。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    ActorTaskPageQuery,
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    DestinationKind,
    FeishuProjectionInput,
    IdentitySource,
    ProjectionErrorCode,
    ProjectionState,
    ScopeTaskPageQuery,
    StoredTaskPage,
    StoredTaskRead,
    TaskStatus,
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


@pytest.mark.parametrize(
    "updates",
    [
        {"task_version": -1},
        {"task_version": True},
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

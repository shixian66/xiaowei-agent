"""Web 任务 API 只传递可信身份并投影安全任务字段。"""

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from xiaowei_agent.application.channel_access import (
    TASK_DETAIL_PREVIEW_LIMIT,
    TASK_SUMMARY_PREVIEW_LIMIT,
    AccessibleTask,
    TaskAccessNotFoundError,
    TaskAccessQuery,
    TaskAccessSnapshotUnavailableError,
    TaskListQuery,
    TaskPage,
    TaskSummary,
)
from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionForbiddenError,
    ChannelSubmitCommand,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    ReadinessReport,
    RenderPayload,
    RenderSection,
    TaskStatus,
    TaskView,
    task_query_path,
)
from xiaowei_agent.interfaces.web_app import SESSION_COOKIE_NAME, create_app
from xiaowei_agent.interfaces.web_auth import (
    AuthenticatedWebSession,
    WebAuthenticationError,
    WebCsrfError,
    WebOriginError,
)
from xiaowei_agent.interfaces.web_models import (
    WebTaskDetail,
    WebTaskSubmitRequest,
    WebTaskSummary,
)
from xiaowei_agent.persistence import IdempotencyConflictError
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.trace import get_trace_id

_COOKIE = "session_value_for_web_task_api"
_CSRF = "a" * 64
_NOW = dt.datetime(2026, 9, 9, 9, 30, tzinfo=dt.UTC)


def _field_max_length(model: type[Any], field_name: str) -> int | None:
    for constraint in model.model_fields[field_name].metadata:
        maximum = getattr(constraint, "max_length", None)
        if isinstance(maximum, int):
            return maximum
    return None


def _principal(
    *, permissions: frozenset[ChannelPermission] | None = None
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="subject-alice",
        permissions=permissions
        if permissions is not None
        else frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            }
        ),
    )


class _Auth:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal
        self.trace_ids: list[str | None] = []

    async def authenticate(self, *, session_cookie: str | None) -> AuthenticatedWebSession:
        self.trace_ids.append(get_trace_id())
        if session_cookie != _COOKIE:
            raise WebAuthenticationError
        return AuthenticatedWebSession(principal=self.principal, csrf_token=_CSRF)

    def validate_state_change(
        self, *, session_cookie: str, origin: str | None, csrf_token: str | None
    ) -> None:
        if origin != "https://ops.example.test":
            raise WebOriginError
        if session_cookie != _COOKIE or csrf_token != _CSRF:
            raise WebCsrfError

    async def start_login(self) -> Any:  # pragma: no cover - 其他契约已覆盖
        raise AssertionError("not used")

    async def complete_login(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def logout(self, *, session_cookie: str | None) -> None:  # pragma: no cover
        raise AssertionError(session_cookie)


class _Probe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True,
            revision_matches_head=True,
            assembled=True,
        )


class _Access:
    def __init__(self) -> None:
        self.list_result = TaskPage(items=(), next_created_seq=None)
        self.detail_result: AccessibleTask | None = None
        self.list_error: Exception | None = None
        self.detail_error: Exception | None = None
        self.list_calls: list[TaskListQuery] = []
        self.detail_calls: list[TaskAccessQuery] = []
        self.trace_ids: list[str | None] = []

    async def list_tasks(self, *, query: TaskListQuery) -> TaskPage:
        self.trace_ids.append(get_trace_id())
        self.list_calls.append(query)
        if self.list_error is not None:
            raise self.list_error
        return self.list_result

    async def get_task(self, *, query: TaskAccessQuery) -> AccessibleTask:
        self.trace_ids.append(get_trace_id())
        self.detail_calls.append(query)
        if self.detail_error is not None:
            raise self.detail_error
        if self.detail_result is None:
            raise AssertionError("detail result not configured")
        return self.detail_result


class _Submissions:
    def __init__(self, view: TaskView) -> None:
        self.view = view
        self.error: Exception | None = None
        self.calls: list[ChannelSubmitCommand] = []
        self.trace_ids: list[str | None] = []
        self.authentication_trace_ids: list[str | None] = []

    async def submit(self, *, command: ChannelSubmitCommand) -> Any:
        self.trace_ids.append(get_trace_id())
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            task_view=self.view,
            parent_task_id=command.parent_task_id,
        )


def _view(status: TaskStatus = TaskStatus.CREATED) -> TaskView:
    render = None
    if status is TaskStatus.SUCCEEDED:
        render = RenderPayload(
            answer="查询完成。",
            sections=(
                RenderSection(
                    title="证据摘要",
                    body="安全证据正文",
                    refs=("evidence:section-1",),
                ),
            ),
            next_steps=("继续观察。",),
            status=status,
            refs=("evidence:root-1",),
        )
    return TaskView(
        task_id="task-1",
        status=status,
        render=render,
        query_path=task_query_path("task-1"),
    )


def _settings() -> Settings:
    return Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        feishu_identity_file="/run/config/feishu-identities.json",
        web_detail_base_url="https://ops.example.test",
    )


def _client(
    *,
    principal: AuthenticatedPrincipal | None = None,
    access: _Access | None = None,
    submissions: _Submissions | None = None,
    raise_app_exceptions: bool = False,
) -> tuple[httpx.AsyncClient, _Access, _Submissions]:
    access = access or _Access()
    submissions = submissions or _Submissions(_view())
    auth = _Auth(principal or _principal())
    submissions.authentication_trace_ids = auth.trace_ids
    app = create_app(
        auth=auth,
        settings=_settings(),
        readiness=_Probe(),
        task_access=access,
        submissions=submissions,
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app,
            raise_app_exceptions=raise_app_exceptions,
        ),
        base_url="https://ops.example.test",
        cookies={SESSION_COOKIE_NAME: _COOKIE},
    )
    return client, access, submissions


async def test_list_tasks_only_safe_summary_fields_and_server_cursor() -> None:
    access = _Access()
    access.list_result = TaskPage(
        items=(
            TaskSummary(
                task_id="task-1",
                status=TaskStatus.RUNNING,
                request_preview="检查最近三十分钟慢查询",
                submitted_at=_NOW,
            ),
        ),
        next_created_seq=41,
    )
    client, _, _ = _client(access=access)

    async with client:
        response = await client.get(
            "/app/api/tasks", params={"before_created_seq": 42, "limit": 20}
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "task_id": "task-1",
                "status": "running",
                "request_preview": "检查最近三十分钟慢查询",
                "submitted_at": "2026-09-09T09:30:00Z",
                "detail_path": "/app/tasks/task-1",
            }
        ],
        "next_created_seq": 41,
    }
    assert access.list_calls == [
        TaskListQuery(
            principal=_principal(), before_created_seq=42, limit=20
        )
    ]
    assert "query_path" not in response.text


def test_web_preview_models_share_the_application_length_contract() -> None:
    assert (
        _field_max_length(WebTaskSummary, "request_preview")
        == TASK_SUMMARY_PREVIEW_LIMIT
    )
    assert (
        _field_max_length(WebTaskDetail, "request_preview")
        == TASK_DETAIL_PREVIEW_LIMIT
    )


def test_web_submit_parent_is_optional_and_strict() -> None:
    assert WebTaskSubmitRequest(
        text="继续分析",
        client_submission_id="browser-parent-0001",
    ).parent_task_id is None
    assert WebTaskSubmitRequest(
        text="继续分析",
        client_submission_id="browser-parent-0001",
        parent_task_id="task-parent",
    ).parent_task_id == "task-parent"
    for invalid in ("", " task-parent ", 1, b"task-parent"):
        with pytest.raises(ValueError):
            WebTaskSubmitRequest(
                text="继续分析",
                client_submission_id="browser-parent-0001",
                parent_task_id=invalid,
            )


async def test_detail_preserves_complete_safe_render_without_internal_query_path() -> None:
    access = _Access()
    access.detail_result = AccessibleTask(
        task_view=_view(TaskStatus.SUCCEEDED),
        request_preview="检查最近三十分钟慢查询",
        submitted_at=_NOW,
        task_version=7,
    )
    client, _, _ = _client(access=access)

    async with client:
        response = await client.get("/app/api/tasks/task-1")

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "task-1",
        "status": "succeeded",
        "request_preview": "检查最近三十分钟慢查询",
        "submitted_at": "2026-09-09T09:30:00Z",
        "task_version": 7,
        "detail_path": "/app/tasks/task-1",
        "render": {
            "answer": "查询完成。",
            "sections": [
                {
                    "title": "证据摘要",
                    "body": "安全证据正文",
                    "refs": ["evidence:section-1"],
                }
            ],
            "next_steps": ["继续观察。"],
            "status": "succeeded",
            "refs": ["evidence:root-1"],
        },
    }
    assert access.detail_calls == [
        TaskAccessQuery(principal=_principal(), task_id="task-1")
    ]
    assert "query_path" not in response.text
    assert "facts" not in response.text


async def test_child_detail_exposes_only_its_parent_task_reference() -> None:
    access = _Access()
    access.detail_result = AccessibleTask(
        task_view=_view(TaskStatus.SUCCEEDED),
        request_preview="继续分析",
        submitted_at=_NOW,
        task_version=8,
        parent_task_id="task-parent",
    )
    client, _, _ = _client(access=access)

    async with client:
        response = await client.get("/app/api/tasks/task-1")

    assert response.status_code == 200
    assert response.json()["parent_task_id"] == "task-parent"
    assert "parent_submission" not in response.text
    assert "parent_binding" not in response.text


async def test_submit_constructs_web_command_from_server_authority(caplog) -> None:
    submissions = _Submissions(_view(TaskStatus.CREATED))
    client, _, _ = _client(submissions=submissions)

    with caplog.at_level(logging.INFO, logger="xiaowei_agent.interfaces.web_app"):
        async with client:
            response = await client.post(
                "/app/api/tasks",
                json={
                    "text": "检查最近三十分钟慢查询",
                    "client_submission_id": "browser-request-0001",
                },
                headers={
                    "origin": "https://ops.example.test",
                    "x-csrf-token": _CSRF,
                    "x-trace-id": "f" * 32,
                },
            )

    assert response.status_code == 202
    assert response.json() == {
        "task_id": "task-1",
        "status": "created",
        "detail_path": "/app/tasks/task-1",
    }
    assert len(submissions.calls) == 1
    command = submissions.calls[0]
    assert command.principal == _principal()
    assert command.channel.value == "web"
    assert command.text == "检查最近三十分钟慢查询"
    assert command.client_submission_ref == "browser-request-0001"
    assert command.conversation_ref is None
    assert command.request_id == f"web:{command.trace_id}"
    assert submissions.trace_ids == [command.trace_id]
    assert submissions.authentication_trace_ids == [command.trace_id]
    assert command.trace_id != "f" * 32
    assert command.policy_revision == "policy-2026-09-01"
    assert command.submitted_at == _NOW
    records = [
        record
        for record in caplog.records
        if record.name == "xiaowei_agent.interfaces.web_app"
    ]
    assert len(records) == 1
    assert records[0].route_class == "task_api"
    assert records[0].outcome == "ok"
    assert records[0].trace_id == command.trace_id


async def test_submit_forwards_explicit_parent_and_returns_its_source() -> None:
    submissions = _Submissions(_view(TaskStatus.CREATED))
    client, _, _ = _client(submissions=submissions)

    async with client:
        response = await client.post(
            "/app/api/tasks",
            json={
                "text": "继续分析这个任务",
                "client_submission_id": "browser-parent-0002",
                "parent_task_id": "task-parent",
            },
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": _CSRF,
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "task_id": "task-1",
        "status": "created",
        "detail_path": "/app/tasks/task-1",
        "parent_task_id": "task-parent",
    }
    assert submissions.calls[0].parent_task_id == "task-parent"


async def test_submit_rejects_non_json_media_type_before_submission() -> None:
    client, _, submissions = _client()

    async with client:
        response = await client.post(
            "/app/api/tasks",
            content=(
                b'{"text":"inspect","client_submission_id":'
                b'"browser-request-media-1"}'
            ),
            headers={
                "content-type": "text/plain",
                "origin": "https://ops.example.test",
                "x-csrf-token": _CSRF,
            },
        )

    assert response.status_code == 415
    assert response.json() == {"error": {"code": "unsupported_media_type"}}
    assert submissions.calls == []


async def test_submit_rejects_duplicate_keys_and_non_json_numbers() -> None:
    client, _, submissions = _client()
    headers = {
        "content-type": "application/json",
        "origin": "https://ops.example.test",
        "x-csrf-token": _CSRF,
    }
    bodies = (
        (
            b'{"text":"first","text":"second","client_submission_id":'
            b'"browser-request-shape-1"}'
        ),
        (
            b'{"text":"inspect","client_submission_id":'
            b'"browser-request-shape-2","value":NaN}'
        ),
    )

    async with client:
        for body in bodies:
            response = await client.post(
                "/app/api/tasks", content=body, headers=headers
            )
            assert response.status_code == 400
            assert response.json() == {"error": {"code": "invalid_request"}}

    assert submissions.calls == []


async def test_task_errors_map_to_closed_http_semantics() -> None:
    cases: tuple[tuple[str, Exception, int, str], ...] = (
        ("detail", TaskAccessNotFoundError(), 404, "not_found"),
        (
            "detail",
            TaskAccessSnapshotUnavailableError(),
            503,
            "unavailable",
        ),
        (
            "submit",
            ChannelSubmissionForbiddenError(),
            403,
            "forbidden",
        ),
        (
            "submit",
            IdempotencyConflictError("conflict"),
            409,
            "idempotency_conflict",
        ),
        (
            "detail",
            PersistenceUnavailableError(
                category=PersistenceUnavailableCategory.CONNECT
            ),
            503,
            "unavailable",
        ),
    )
    for target, error, status, code in cases:
        access = _Access()
        submissions = _Submissions(_view())
        if target == "detail":
            access.detail_error = error
        else:
            submissions.error = error
        client, _, _ = _client(
            access=access,
            submissions=submissions,
            raise_app_exceptions=True,
        )
        async with client:
            if target == "detail":
                response = await client.get("/app/api/tasks/hidden-task")
            else:
                response = await client.post(
                    "/app/api/tasks",
                    json={
                        "text": "检查慢查询",
                        "client_submission_id": "browser-request-0002",
                    },
                    headers={
                        "origin": "https://ops.example.test",
                        "x-csrf-token": _CSRF,
                    },
                )
        assert response.status_code == status
        assert response.json() == {"error": {"code": code}}


async def test_invalid_or_replayed_cursor_is_rejected_without_service_call() -> None:
    client, access, _ = _client()
    async with client:
        for query in (
            "before_created_seq=0",
            "before_created_seq=-1",
            "before_created_seq=abc",
            "before_created_seq=9999999999999999999",
            "before_created_seq=4&before_created_seq=4",
        ):
            response = await client.get(f"/app/api/tasks?{query}")
            assert response.status_code == 404
            assert response.json() == {"error": {"code": "not_found"}}
    assert access.list_calls == []

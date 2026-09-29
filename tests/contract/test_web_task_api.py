"""Web 任务 API 只传递可信身份并投影安全任务字段。"""

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from tests.fakes.admin_identity import UnusedAdminIdentity
from tests.fakes.integration_config import AbsentIntegrationConfig
from tests.fakes.web_auth import EmptyProviderState, NoLocalAdmin

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
    ChannelParentNotFoundError,
    ChannelSubmissionForbiddenError,
    ChannelSubmitCommand,
)
from xiaowei_agent.application.task_view_runtime import ClarificationIntegrityError
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AdminCapability,
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    IdentitySource,
    ProductRole,
    ReadinessReport,
    RenderPayload,
    RenderSection,
    TaskLookup,
    TaskStatus,
    TaskView,
    WebMode,
    task_query_path,
)
from xiaowei_agent.governance.sql_message import recognize_sql_message
from xiaowei_agent.interfaces.web_app import create_app, session_cookie_name
from xiaowei_agent.interfaces.web_auth import (
    AuthenticatedWebSession,
    WebAuthenticationError,
    WebCsrfError,
    WebOriginError,
    web_csrf_token,
)
from xiaowei_agent.interfaces.web_models import (
    WebTaskDetail,
    WebTaskSubmitRequest,
    WebTaskSummary,
    web_task_detail_path,
)
from xiaowei_agent.persistence import IdempotencyConflictError
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.trace import get_trace_id

_SESSION_COOKIE_NAME = session_cookie_name(WebMode.HTTPS)

_COOKIE = "session_value_for_web_task_api"
# CSRF token 由 session cookie 派生，且派生实现只有一份（web_auth.web_csrf_token）。
# 写成固定字面量会让替身与真实实现脱钩，路由层换成公共函数后立刻 403。
_CSRF = web_csrf_token(_COOKIE)
_NOW = dt.datetime(2026, 9, 9, 9, 30, tzinfo=dt.UTC)


def _field_max_length(model: type[Any], field_name: str) -> int | None:
    for constraint in model.model_fields[field_name].metadata:
        maximum = getattr(constraint, "max_length", None)
        if isinstance(maximum, int):
            return maximum
    return None


def test_web_task_detail_path_encodes_reserved_characters_without_validating_task_id() -> None:
    assert web_task_detail_path("task/with?reserved#chars%") == (
        "/app/tasks/task%2Fwith%3Freserved%23chars%25"
    )


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
        return AuthenticatedWebSession(
            user_id="user-alice",
            principal=self.principal,
            role=ProductRole.OPERATOR,
            admin_capabilities=frozenset[AdminCapability](),
            csrf_token=_CSRF,
        )

    def validate_state_change(
        self, *, session_cookie: str, origin: str | None, csrf_token: str | None
    ) -> None:
        if origin != "https://ops.example.test":
            raise WebOriginError
        if session_cookie != _COOKIE or csrf_token != _CSRF:
            raise WebCsrfError

    async def start_login(self, **_: object) -> Any:  # pragma: no cover
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
            clarification_parent_task_id=command.clarification_parent_task_id,
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
        web_public_origin="https://ops.example.test",
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
        local_admin_auth=NoLocalAdmin(),
        oauth_available=True,
        settings=_settings(),
        readiness=_Probe(),
        task_access=access,
        submissions=submissions,
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
        provider_state=EmptyProviderState(),
        integration_config=AbsentIntegrationConfig(),
        admin_identity=UnusedAdminIdentity(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app,
            raise_app_exceptions=raise_app_exceptions,
        ),
        base_url="https://ops.example.test",
        cookies={_SESSION_COOKIE_NAME: _COOKIE},
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
    ).clarification_parent_task_id is None
    assert WebTaskSubmitRequest(
        text="继续分析",
        client_submission_id="browser-parent-0001",
        clarification_parent_task_id="task-parent",
    ).clarification_parent_task_id == "task-parent"
    assert WebTaskSubmitRequest(
        text="继续分析",
        client_submission_id="browser-parent-0001",
        clarification_parent_task_id="a" + ("-" * 199),
    ).clarification_parent_task_id == "a" + ("-" * 199)
    for invalid in (
        "",
        " task-parent ",
        "_task-parent",
        "-task-parent",
        "task/parent",
        "task?parent",
        "a" * 201,
        ("a" * 200) + "\n",
        1,
        b"task-parent",
    ):
        with pytest.raises(ValueError):
            WebTaskSubmitRequest(
                text="继续分析",
                client_submission_id="browser-parent-0001",
                clarification_parent_task_id=invalid,
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
        clarification_parent_task_id="task-parent",
    )
    client, _, _ = _client(access=access)

    async with client:
        response = await client.get("/app/api/tasks/task-1")

    assert response.status_code == 200
    assert response.json()["clarification_parent_task_id"] == "task-parent"
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
                "clarification_parent_task_id": "task-parent",
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
        "clarification_parent_task_id": "task-parent",
    }
    assert submissions.calls[0].clarification_parent_task_id == "task-parent"


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
            ChannelParentNotFoundError(),
            404,
            "not_found",
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
        (
            "detail",
            ClarificationIntegrityError(),
            500,
            "clarification.integrity_error",
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


# --- F1：目标选择作答时 SQL 已过期或不可读（设计 §9.4、计划 Task 8） --------------

_F1_HEADERS = {"origin": "https://ops.example.test", "x-csrf-token": _CSRF}


async def _f1_web_stack(store: Any, memory_state: Any, clock: Any) -> Any:
    from xiaowei_agent.application.channel_access import TaskAccessService
    from xiaowei_agent.application.channel_submission import ChannelSubmissionService
    from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
    from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
    from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
    from xiaowei_agent.persistence.fake import InMemoryChannelStore
    from xiaowei_agent.persistence.plans import InMemoryPlanStore

    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),  # type: ignore[arg-type]
        recognize_sql=recognize_sql_message,
    )
    channels = InMemoryChannelStore(clock=clock, state=memory_state)
    access = TaskAccessService(
        runtime=runtime, task_store=store, channel_store=channels, membership=None
    )
    submissions = ChannelSubmissionService(
        runtime=runtime, channel_store=channels, web_parent_access=access
    )
    return channels, submissions


async def _f1_web_parent(
    store: Any,
    records: Any,
    channels: Any,
    context: Any,
    *,
    key: str,
    sql_ref: str | None = None,
    sql_hash: str | None = None,
) -> Any:
    """Web 提交的 SQL 任务停在目标选择追问；记录可指向任意（含不可读的）SQL。"""
    from tests.suites.task_store import park_for_target_selection

    from xiaowei_agent.persistence.channel import BindTaskCommand
    from xiaowei_agent.persistence.store import SqlQuerySubmitCommand

    submitted = await store.submit_sql_query(
        command=SqlQuerySubmitCommand(
            context=context,
            sql_bytes=b"SELECT id FROM orders",
            idempotency_key=key,
            as_of=_NOW,
        )
    )
    task = submitted.task
    own = await store.get_submission(
        lookup=TaskLookup(
            task_id=task.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    await channels.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            channel=ChannelKind.WEB,
            initiator_subject_ref="subject-alice",
            conversation_ref=None,
            source_event_ref=f"event-{key}",
            created_at=_NOW,
        )
    )
    await park_for_target_selection(
        store,
        records,
        task.task_id,
        sql_ref=own.sql_ref if sql_ref is None else sql_ref,
        sql_hash=own.sql_hash if sql_hash is None else sql_hash,
    )
    return task, own


async def _answer(client: httpx.AsyncClient, parent_id: str, key: str) -> httpx.Response:
    return await client.post(
        "/app/api/tasks",
        json={
            "text": "Orders",
            "client_submission_id": f"browser-answer-{key}-0001",
            "clarification_parent_task_id": parent_id,
        },
        headers=_F1_HEADERS,
    )


async def test_expired_sql_answer_is_409_without_consuming_the_parent(
    store, memory_state, clock, context, clarification_record_store
) -> None:
    channels, submissions = await _f1_web_stack(store, memory_state, clock)
    parent, _ = await _f1_web_parent(
        store, clarification_record_store, channels, context, key="f1-exp"
    )
    tasks_before = set(memory_state.tasks)
    bindings_before = dict(memory_state.channel_bindings)
    clock.advance(seconds=24 * 3600 + 1)
    client, _, _ = _client(submissions=submissions)

    async with client:
        response = await _answer(client, parent.task_id, "exp")

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "sql_artifact.expired"}}
    assert memory_state.tasks[parent.task_id].status is TaskStatus.CLARIFICATION_REQUIRED
    assert set(memory_state.tasks) == tasks_before
    assert dict(memory_state.channel_bindings) == bindings_before


async def test_unreadable_sql_answers_are_one_indistinguishable_409(
    store, memory_state, clock, context, clarification_record_store
) -> None:
    from xiaowei_agent.persistence.store import SqlQuerySubmitCommand

    channels, submissions = await _f1_web_stack(store, memory_state, clock)
    bob = context.model_copy(update={"actor": "bob"})
    foreign = await store.submit_sql_query(
        command=SqlQuerySubmitCommand(
            context=bob, sql_bytes=b"SELECT 2", idempotency_key="bob-sql", as_of=_NOW
        )
    )
    foreign_submission = await store.get_submission(
        lookup=TaskLookup(
            task_id=foreign.task.task_id,
            tenant_id=bob.tenant_id,
            environment_id=bob.environment_id,
        )
    )
    parents = []
    for key, sql_ref, sql_hash in (
        ("f1-missing", "no-such-ref", None),
        ("f1-scope", foreign_submission.sql_ref, foreign_submission.sql_hash),
        ("f1-hash", None, "f" * 64),
    ):
        parent, _ = await _f1_web_parent(
            store,
            clarification_record_store,
            channels,
            context,
            key=key,
            sql_ref=sql_ref,
            sql_hash=sql_hash,
        )
        parents.append(parent)
    tasks_before = set(memory_state.tasks)
    bindings_before = dict(memory_state.channel_bindings)
    client, _, _ = _client(submissions=submissions)

    async with client:
        responses = [
            await _answer(client, parent.task_id, f"unavailable-{index}")
            for index, parent in enumerate(parents)
        ]

    assert {response.status_code for response in responses} == {409}
    assert {response.content for response in responses} == {
        b'{"error":{"code":"sql_artifact.unavailable"}}'
    }
    for parent in parents:
        assert memory_state.tasks[parent.task_id].status is (
            TaskStatus.CLARIFICATION_REQUIRED
        )
    assert set(memory_state.tasks) == tasks_before
    assert dict(memory_state.channel_bindings) == bindings_before


async def test_a_readable_sql_answer_creates_the_child(
    store, memory_state, clock, context, clarification_record_store
) -> None:
    channels, submissions = await _f1_web_stack(store, memory_state, clock)
    parent, _ = await _f1_web_parent(
        store, clarification_record_store, channels, context, key="f1-ok"
    )
    client, _, _ = _client(submissions=submissions)

    async with client:
        response = await _answer(client, parent.task_id, "ok")

    assert response.status_code == 202
    body = response.json()
    assert body["clarification_parent_task_id"] == parent.task_id
    assert body["task_id"] in memory_state.tasks


# --- F1：聊天框文本上限（设计 §5.1） ---------------------------------------------


async def test_web_chat_accepts_long_sql_but_not_long_conversation(
    store, memory_state, clock
) -> None:
    _, submissions = await _f1_web_stack(store, memory_state, clock)
    client, _, _ = _client(submissions=submissions)
    long_sql = "SELECT '" + "x" * 20_000 + "' AS a"

    async with client:
        sql = await client.post(
            "/app/api/tasks",
            json={"text": long_sql, "client_submission_id": "browser-long-sql-0001"},
            headers=_F1_HEADERS,
        )
        chat = await client.post(
            "/app/api/tasks",
            json={"text": "慢" * 8193, "client_submission_id": "browser-long-chat-0001"},
            headers=_F1_HEADERS,
        )
        over = await client.post(
            "/app/api/tasks",
            json={"text": "中" * 21_846, "client_submission_id": "browser-over-byte-0001"},
            headers=_F1_HEADERS,
        )

    assert sql.status_code == 202
    (artifact,) = memory_state.sql_artifacts.values()
    assert artifact.sql_bytes == long_sql.encode()
    assert chat.status_code == 413
    assert chat.json() == {"error": {"code": "payload_too_large"}}
    assert over.status_code == 400
    assert over.json() == {"error": {"code": "invalid_request"}}
    assert len(memory_state.tasks) == 1


# --- F1：SQL 形状即 SQL 消息，不进入普通对话（审查 P1-1） ------------------------


@pytest.mark.parametrize(
    "text",
    [
        "SHOW USERS",
        "SHOW BACKENDS; SHOW FRONTENDS",
        "SHOW /*+ SET_VAR(query_timeout=1) */ BACKENDS",
        "-- comment\nSELECT 1; DROP TABLE t",
        "```sql\n/*+ SET_VAR(a=1) */ SELECT 1\n```",
        # 像 SQL 就是 SQL：写不完整、签名补不完的也只进 SqlArtifact。
        "SELECT 'password=hunter2",
        "SELECT TRUE AND TRUE",
        "show data for yesterday",
    ],
)
async def test_web_chat_stores_sql_shaped_text_only_as_an_artifact(
    store, memory_state, clock, text: str
) -> None:
    _, submissions = await _f1_web_stack(store, memory_state, clock)
    client, _, _ = _client(submissions=submissions)

    async with client:
        response = await client.post(
            "/app/api/tasks",
            json={"text": text, "client_submission_id": "browser-sql-shape-0001"},
            headers=_F1_HEADERS,
        )

    assert response.status_code == 202
    (submission,) = memory_state.submissions.values()
    assert submission.input_kind == "sql_artifact"
    (artifact,) = memory_state.sql_artifacts.values()
    assert artifact.sql_bytes.decode().strip() in text


async def test_web_chat_conversation_control_stays_a_conversation(
    store, memory_state, clock
) -> None:
    _, submissions = await _f1_web_stack(store, memory_state, clock)
    client, _, _ = _client(submissions=submissions)

    async with client:
        response = await client.post(
            "/app/api/tasks",
            json={"text": "show 一下慢查询", "client_submission_id": "browser-chat-shape-0001"},
            headers=_F1_HEADERS,
        )

    assert response.status_code == 202
    (submission,) = memory_state.submissions.values()
    assert submission.input_kind == "conversation"
    assert memory_state.sql_artifacts == {}


async def test_web_sql_shaped_answer_is_422_without_consuming_the_parent(
    store, memory_state, clock, context, clarification_record_store
) -> None:
    channels, submissions = await _f1_web_stack(store, memory_state, clock)
    parent, _ = await _f1_web_parent(
        store, clarification_record_store, channels, context, key="f1-sql-answer"
    )
    tasks_before = set(memory_state.tasks)
    client, _, _ = _client(submissions=submissions)

    async with client:
        response = await client.post(
            "/app/api/tasks",
            json={
                "text": "SHOW USERS",
                "client_submission_id": "browser-sql-answer-0001",
                "clarification_parent_task_id": parent.task_id,
            },
            headers=_F1_HEADERS,
        )

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "sql_message.not_accepted"}}
    assert memory_state.tasks[parent.task_id].status is TaskStatus.CLARIFICATION_REQUIRED
    assert set(memory_state.tasks) == tasks_before

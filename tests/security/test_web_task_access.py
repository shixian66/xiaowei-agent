"""Web 任务路由不能绕过 session、CSRF 或服务端 principal。"""

import pytest
from tests.conftest import make_envelope, make_submission
from tests.contract.test_web_task_api import (
    _COOKIE,
    _CSRF,
    _client,
    _principal,
)

from xiaowei_agent.application.channel_access import TaskAccessService
from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionService,
)
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.contracts import ChannelKind, ChannelPermission
from xiaowei_agent.persistence.channel import BindTaskCommand
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryChannelStore
from xiaowei_agent.persistence.plans import InMemoryPlanStore

pytestmark = pytest.mark.security


async def test_all_task_routes_require_a_valid_session() -> None:
    client, access, submissions = _client()
    client.cookies.clear()
    async with client:
        for method, path in (
            ("GET", "/app/api/tasks"),
            ("GET", "/app/api/tasks/task-1"),
            ("GET", "/app/tasks/task-1"),
            ("POST", "/app/api/tasks"),
        ):
            kwargs = {}
            if method == "POST":
                kwargs = {
                    "json": {
                        "text": "检查慢查询",
                        "client_submission_id": "browser-request-1001",
                    },
                    "headers": {
                        "origin": "https://ops.example.test",
                        "x-csrf-token": _CSRF,
                    },
                }
            response = await client.request(method, path, **kwargs)
            assert response.status_code == 401
            assert response.json() == {"error": {"code": "unauthorized"}}
    assert access.list_calls == []
    assert access.detail_calls == []
    assert submissions.calls == []


async def test_malformed_detail_id_does_not_bypass_session_gate() -> None:
    client, access, _ = _client()
    client.cookies.clear()

    async with client:
        response = await client.get("/app/tasks/not%20a%20task")

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized"}}
    assert access.detail_calls == []


async def test_submit_rejects_missing_or_wrong_origin_and_csrf_before_service() -> None:
    client, _, submissions = _client()
    body = {
        "text": "检查慢查询",
        "client_submission_id": "browser-request-1002",
    }
    headers = (
        {},
        {"origin": "https://evil.example.test", "x-csrf-token": _CSRF},
        {"origin": "https://ops.example.test", "x-csrf-token": "b" * 64},
    )
    async with client:
        for case in headers:
            response = await client.post("/app/api/tasks", json=body, headers=case)
            assert response.status_code == 403
            assert response.json() == {"error": {"code": "forbidden"}}
    assert submissions.calls == []


async def test_body_cannot_supply_or_override_authority_fields() -> None:
    client, _, submissions = _client()
    forged = {
        "text": "检查慢查询",
        "client_submission_id": "browser-request-1003",
        "tenant_id": "prod-tenant",
        "environment_id": "prod",
        "actor": "admin",
        "permissions": ["admin_all_safe_tasks"],
    }
    async with client:
        response = await client.post(
            "/app/api/tasks",
            json=forged,
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": _CSRF,
            },
        )
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request"}}
    assert submissions.calls == []


async def test_view_only_user_gets_real_route_403_without_submission(
    store, memory_state, clock
) -> None:
    principal = _principal(
        permissions=frozenset({ChannelPermission.VIEW_SAFE_TASK})
    )
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
    )
    submissions = ChannelSubmissionService(
        runtime=runtime,
        channel_store=InMemoryChannelStore(clock=clock, state=memory_state),
    )
    client, _, _ = _client(principal=principal, submissions=submissions)
    async with client:
        response = await client.post(
            "/app/api/tasks",
            json={
                "text": "检查慢查询",
                "client_submission_id": "browser-request-1004",
            },
            headers={
                "origin": "https://ops.example.test",
                "x-csrf-token": _CSRF,
            },
        )
    assert response.status_code == 403
    assert response.json() == {"error": {"code": "forbidden"}}
    assert memory_state.tasks == {}
    assert memory_state.channel_bindings == {}


async def test_duplicate_security_headers_are_rejected_without_submission() -> None:
    client, _, submissions = _client()
    async with client:
        for duplicate in ("origin", "x-csrf-token"):
            headers = [
                ("content-type", "application/json"),
                ("origin", "https://ops.example.test"),
                ("x-csrf-token", _CSRF),
                (
                    duplicate,
                    "https://ops.example.test" if duplicate == "origin" else _CSRF,
                ),
            ]
            response = await client.post(
                "/app/api/tasks",
                content=(
                    b'{"text":"inspect","client_submission_id":'
                    b'"browser-request-1005"}'
                ),
                headers=headers,
            )
            assert response.status_code == 403
            assert response.json() == {"error": {"code": "forbidden"}}
    assert submissions.calls == []


async def test_cookie_name_cannot_be_replayed_twice() -> None:
    client, access, _ = _client()
    client.cookies.clear()
    response = await client.get(
        "/app/api/tasks",
        headers={
            "cookie": (
                f"__Host-xiaowei-session={_COOKIE}; "
                f"__Host-xiaowei-session={_COOKIE}"
            )
        },
    )
    await client.aclose()
    assert response.status_code == 401
    assert access.list_calls == []


async def test_web_route_reuses_scoped_idempotency_and_conflict_semantics(
    store, memory_state, clock
) -> None:
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
    )
    channel_store = InMemoryChannelStore(clock=clock, state=memory_state)
    submissions = ChannelSubmissionService(
        runtime=runtime,
        channel_store=channel_store,
    )
    client, _, _ = _client(submissions=submissions)
    headers = {
        "origin": "https://ops.example.test",
        "x-csrf-token": _CSRF,
    }
    first_body = {
        "text": "检查慢查询",
        "client_submission_id": "browser-idempotency-1",
    }
    async with client:
        first = await client.post("/app/api/tasks", json=first_body, headers=headers)
        replay = await client.post("/app/api/tasks", json=first_body, headers=headers)
        conflict = await client.post(
            "/app/api/tasks",
            json={**first_body, "text": "检查另一项任务"},
            headers=headers,
        )

    assert first.status_code == replay.status_code == 202
    assert first.json()["task_id"] == replay.json()["task_id"]
    assert len(memory_state.tasks) == 1
    assert len(memory_state.channel_bindings) == 1
    assert conflict.status_code == 409
    assert conflict.json() == {"error": {"code": "idempotency_conflict"}}


async def test_group_membership_is_rechecked_and_revocation_becomes_404(
    store, memory_state, clock, context
) -> None:
    task = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="request-group-web",
                idempotency_key="group-web",
                text="群内安全任务",
            ),
        )
    )
    channel_store = InMemoryChannelStore(clock=clock, state=memory_state)
    await channel_store.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id="dev-local",
            environment_id="dev",
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-1",
            source_event_ref="event-group-web",
            created_at=clock(),
        )
    )

    class Membership:
        def __init__(self) -> None:
            self.results = iter((True, False))
            self.calls = 0

        async def is_current_group_member(self, **_: str) -> bool:
            self.calls += 1
            return next(self.results)

    membership = Membership()
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
    )
    access = TaskAccessService(
        runtime=runtime,
        task_store=store,
        channel_store=channel_store,
        membership=membership,
    )
    bob = _principal().model_copy(
        update={"actor": "bob", "subject_ref": "subject-bob"}
    )
    client, _, _ = _client(principal=bob, access=access)

    async with client:
        first = await client.get(f"/app/api/tasks/{task.task_id}")
        revoked = await client.get(f"/app/api/tasks/{task.task_id}")

    assert first.status_code == 200
    assert revoked.status_code == 404
    assert revoked.json() == {"error": {"code": "not_found"}}
    assert membership.calls == 2

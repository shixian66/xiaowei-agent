"""W3-lite Admin 身份 API 的真实 ASGI 边界。"""

import datetime as dt
import json
from typing import Any

import httpx
import pytest
from tests.fakes.integration_config import AbsentIntegrationConfig
from tests.fakes.web_auth import EmptyProviderState

from xiaowei_agent.application.admin_identity import (
    AdminIdentityConflictError,
    AdminIdentityNotFoundError,
    AdminIdentityUnavailableError,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    LOCAL_ADMIN_USER_ID,
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditTargetKind,
    AdminCapability,
    AuthenticatedPrincipal,
    IdentitySource,
    ProductRole,
    ReadinessReport,
    UserStatus,
    WebMode,
)
from xiaowei_agent.contracts.admin_audit import AdminAuditEffect
from xiaowei_agent.contracts.admin_identity import (
    AdminAuditSummary,
    AdminAuditViewPage,
    AdminPendingActivationPage,
    AdminPendingActivationSummary,
    AdminUserSummary,
    AdminUserViewPage,
)
from xiaowei_agent.contracts.enums import ActivationSource
from xiaowei_agent.governance.product_roles import (
    admin_capabilities,
    channel_permissions,
)
from xiaowei_agent.interfaces.local_admin_auth import (
    LOCAL_ADMIN_PRINCIPAL,
    LocalAdminAuthenticationError,
    LocalAdminSession,
)
from xiaowei_agent.interfaces.web_app import create_app, session_cookie_name
from xiaowei_agent.interfaces.web_auth import (
    AuthenticatedWebSession,
    WebAuthenticationError,
    web_csrf_token,
)

_ORIGIN = "https://ops.example.test"
_LOCAL_COOKIE = "local_admin_session_cookie"
_FEISHU_COOKIE = "feishu_admin_session_cookie"
_COOKIE_NAME = session_cookie_name(WebMode.HTTPS)
_NOW = dt.datetime(2026, 9, 24, 10, 30, tzinfo=dt.UTC)


class _Probe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True, revision_matches_head=True, assembled=True
        )


class _Unused:
    def __getattr__(self, name: str) -> Any:  # pragma: no cover - 误调用即失败
        raise AssertionError(name)


class _LocalAuth:
    async def authenticate(self, *, session_cookie: str | None) -> LocalAdminSession:
        if session_cookie != _LOCAL_COOKIE:
            raise LocalAdminAuthenticationError
        return LocalAdminSession(
            principal=LOCAL_ADMIN_PRINCIPAL,
            role=ProductRole.ADMIN,
            admin_capabilities=admin_capabilities(
                role=ProductRole.ADMIN, source=IdentitySource.LOCAL_ADMIN
            ),
            csrf_token=web_csrf_token(_LOCAL_COOKIE),
        )

    async def login(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def change_password(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def logout(self, *, session_cookie: str) -> bool:  # pragma: no cover
        raise AssertionError(session_cookie)


class _FeishuAuth:
    def __init__(
        self,
        *,
        role: ProductRole = ProductRole.ADMIN,
        capabilities: frozenset[AdminCapability] | None = None,
    ) -> None:
        self.role = role
        self.capabilities = capabilities

    async def authenticate(
        self, *, session_cookie: str | None
    ) -> AuthenticatedWebSession:
        if session_cookie != _FEISHU_COOKIE:
            raise WebAuthenticationError
        principal = AuthenticatedPrincipal(
            tenant_id="dev-local",
            environment_id="dev",
            actor="feishu-admin@example.test",
            source=IdentitySource.FEISHU,
            subject_ref="ou_" + "private-subject",
            permissions=channel_permissions(role=self.role),
        )
        return AuthenticatedWebSession(
            user_id="feishu-admin-user",
            principal=principal,
            role=self.role,
            admin_capabilities=(
                self.capabilities
                if self.capabilities is not None
                else admin_capabilities(role=self.role, source=IdentitySource.FEISHU)
            ),
            csrf_token=web_csrf_token(_FEISHU_COOKIE),
        )

    async def start_login(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def complete_login(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def logout(self, **_: object) -> Any:  # pragma: no cover
        raise AssertionError("not used")


class _AdminIdentity:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None

    def _record(self, name: str, values: dict[str, object]) -> None:
        self.calls.append((name, values))
        if self.error is not None:
            raise self.error

    async def list_users(self, **values: object) -> AdminUserViewPage:
        self._record("list_users", values)
        return AdminUserViewPage(
            items=(
                AdminUserSummary(
                    user_id="user-1",
                    actor="operator@example.test",
                    display_name="Operator One",
                    status=UserStatus.ACTIVE,
                    role=ProductRole.OPERATOR,
                    feishu_bound=True,
                    updated_at=_NOW,
                ),
            ),
            next_after_actor="operator@example.test",
        )

    async def list_pending_activations(
        self, **values: object
    ) -> AdminPendingActivationPage:
        self._record("list_pending_activations", values)
        return AdminPendingActivationPage(
            items=(
                AdminPendingActivationSummary(
                    request_id="request-1",
                    subject_hint="申请 · 12ab34cd",
                    source=ActivationSource.WEB_LOGIN,
                    requested_at=_NOW,
                    expires_at=_NOW + dt.timedelta(hours=1),
                ),
            )
        )

    async def list_audit(self, **values: object) -> AdminAuditViewPage:
        self._record("list_audit", values)
        return AdminAuditViewPage(
            items=(
                AdminAuditSummary(
                    event_id="event-1",
                    actor="admin",
                    auth_source=IdentitySource.LOCAL_ADMIN,
                    action=AdminAuditAction.USER_STATUS_CHANGED,
                    target_kind=AdminAuditTargetKind.USER,
                    outcome=AdminAuditOutcome.SUCCEEDED,
                    reason_code=None,
                    effect=AdminAuditEffect(status=UserStatus.DISABLED),
                    created_at=_NOW,
                ),
            )
        )

    async def set_user_status(self, **values: object) -> None:
        self._record("set_user_status", values)

    async def change_user_role(self, **values: object) -> None:
        self._record("change_user_role", values)

    async def approve_activation(self, **values: object) -> None:
        self._record("approve_activation", values)

    async def reject_activation(self, **values: object) -> None:
        self._record("reject_activation", values)


def _app(
    service: _AdminIdentity,
    *,
    feishu_role: ProductRole = ProductRole.ADMIN,
    feishu_capabilities: frozenset[AdminCapability] | None = None,
) -> Any:
    return create_app(
        auth=_FeishuAuth(  # type: ignore[arg-type]
            role=feishu_role,
            capabilities=feishu_capabilities,
        ),
        local_admin_auth=_LocalAuth(),  # type: ignore[arg-type]
        oauth_available=True,
        settings=Settings(
            environment_id="dev",
            web_app_enabled=True,
            feishu_oauth_enabled=True,
            web_public_origin=_ORIGIN,
            api_request_body_limit_bytes=1024,
        ),
        readiness=_Probe(),
        task_access=_Unused(),
        submissions=_Unused(),
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
        provider_state=EmptyProviderState(),
        integration_config=AbsentIntegrationConfig(),
        admin_identity=service,  # type: ignore[arg-type]
    )


def _client(
    app: Any, *, cookie: str | None = None
) -> httpx.AsyncClient:
    cookies = {} if cookie is None else {_COOKIE_NAME: cookie}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=_ORIGIN,
        cookies=cookies,
    )


def _headers(cookie: str, *, csrf: bool = True) -> dict[str, str]:
    result = {"origin": _ORIGIN, "content-type": "application/json"}
    if csrf:
        result["x-csrf-token"] = web_csrf_token(cookie)
    return result


_WRITE_CASES = (
    (
        "/admin/api/users/status",
        {
            "user_id": "user-1",
            "expected_status": "active",
            "expected_role": "user",
            "status": "disabled",
            "confirm": True,
        },
        "set_user_status",
    ),
    (
        "/admin/api/users/role",
        {
            "user_id": "user-1",
            "expected_role": "user",
            "role": "operator",
            "confirm": True,
        },
        "change_user_role",
    ),
    (
        "/admin/api/activations/approve",
        {
            "request_id": "request-1",
            "actor": "user@example.test",
            "display_name": "User One",
            "approved_role": "user",
            "confirm": True,
        },
        "approve_activation",
    ),
    (
        "/admin/api/activations/reject",
        {"request_id": "request-1", "confirm": True},
        "reject_activation",
    ),
)


def test_admin_identity_route_set_is_exact() -> None:
    service = _AdminIdentity()
    app = _app(service)
    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/admin/api/")
        and not route.path.startswith("/admin/api/config")
        # W4b 资源登记属于配置面，由 test_web_app_routes 的全量闭集钉住。
        and not route.path.startswith("/admin/api/resources")
        and route.path != "/admin/api/integration-status"
    }
    assert routes == {
        ("GET", "/admin/api/users"),
        ("POST", "/admin/api/users/status"),
        ("POST", "/admin/api/users/role"),
        ("GET", "/admin/api/activations"),
        ("POST", "/admin/api/activations/approve"),
        ("POST", "/admin/api/activations/reject"),
        ("GET", "/admin/api/audit"),
    }


async def test_identity_routes_require_a_live_admin_with_the_specific_capability() -> None:
    service = _AdminIdentity()
    app = _app(service, feishu_role=ProductRole.OPERATOR)
    async with _client(app) as anonymous:
        assert (await anonymous.get("/admin/api/users")).status_code == 401
    async with _client(app, cookie=_FEISHU_COOKIE) as operator:
        assert (await operator.get("/admin/api/users")).status_code == 403
        assert (await operator.get("/admin/api/audit")).status_code == 403
    assert service.calls == []


async def test_each_identity_capability_is_checked_independently_of_admin_role() -> None:
    users_service = _AdminIdentity()
    users_app = _app(
        users_service,
        feishu_capabilities=frozenset({AdminCapability.VIEW_ADMIN_AUDIT}),
    )
    async with _client(users_app, cookie=_FEISHU_COOKIE) as audit_only_admin:
        denied_users = await audit_only_admin.get("/admin/api/users")
        allowed_audit = await audit_only_admin.get("/admin/api/audit")

    audit_service = _AdminIdentity()
    audit_app = _app(
        audit_service,
        feishu_capabilities=frozenset({AdminCapability.MANAGE_USERS}),
    )
    async with _client(audit_app, cookie=_FEISHU_COOKIE) as users_only_admin:
        allowed_users = await users_only_admin.get("/admin/api/users")
        denied_audit = await users_only_admin.get("/admin/api/audit")

    assert denied_users.status_code == denied_audit.status_code == 403
    assert allowed_users.status_code == allowed_audit.status_code == 200
    assert [call[0] for call in users_service.calls] == ["list_audit"]
    assert [call[0] for call in audit_service.calls] == ["list_users"]


async def test_local_and_feishu_admins_share_identity_routes_but_not_raw_config() -> None:
    service = _AdminIdentity()
    app = _app(service)
    async with _client(app, cookie=_LOCAL_COOKIE) as local:
        local_users = await local.get("/admin/api/users")
    async with _client(app, cookie=_FEISHU_COOKIE) as feishu:
        feishu_users = await feishu.get("/admin/api/users")
        raw_config = [await feishu.get(f"/admin/api/config/{d}") for d in ("ai", "feishu")]

    assert local_users.status_code == feishu_users.status_code == 200
    assert [response.status_code for response in raw_config] == [403, 403]
    local_actor = service.calls[0][1]["actor"]
    feishu_actor = service.calls[1][1]["actor"]
    assert local_actor.user_id == LOCAL_ADMIN_USER_ID
    assert local_actor.auth_source is IdentitySource.LOCAL_ADMIN
    assert feishu_actor.user_id == "feishu-admin-user"
    assert feishu_actor.auth_source is IdentitySource.FEISHU


async def test_safe_pages_do_not_serialize_controlled_identifiers_or_digests() -> None:
    service = _AdminIdentity()
    app = _app(service)
    private_subject = "ou_" + "private-subject"
    fake_secret = "hunter" + "2-plain"
    async with _client(app, cookie=_LOCAL_COOKIE) as client:
        responses = [
            await client.get("/admin/api/users"),
            await client.get("/admin/api/activations"),
            await client.get("/admin/api/audit"),
        ]

    serialized = "".join(response.text for response in responses)
    assert all(response.status_code == 200 for response in responses)
    assert private_subject not in serialized
    assert fake_secret not in serialized
    assert "subject_ref" not in serialized
    assert "target_ref_digest" not in serialized


async def test_admin_query_parsers_reject_ambiguous_or_unbounded_input() -> None:
    service = _AdminIdentity()
    app = _app(service)
    invalid = (
        "/admin/api/users?unknown=1",
        "/admin/api/users?limit=0",
        "/admin/api/users?limit=101",
        "/admin/api/users?limit=abc",
        "/admin/api/users?limit=2&limit=3",
        "/admin/api/activations?before_request_id=request-1",
        "/admin/api/activations?before_requested_at=2026-09-24T10%3A30%3A00Z",
        "/admin/api/audit?before_event_id=event-1",
        "/admin/api/audit?action=not-an-action",
        "/admin/api/audit?outcome=not-an-outcome",
        "/admin/api/audit?target_user_id=user-1&target_request_id=request-1",
    )
    async with _client(app, cookie=_LOCAL_COOKIE) as client:
        responses = [await client.get(path) for path in invalid]

    assert [response.status_code for response in responses] == [400] * len(invalid)
    assert service.calls == []


async def test_admin_query_cursors_and_filters_reach_only_the_safe_service_surface() -> None:
    service = _AdminIdentity()
    app = _app(service)
    async with _client(app, cookie=_LOCAL_COOKIE) as client:
        activations = await client.get(
            "/admin/api/activations",
            params={
                "before_requested_at": "2026-09-24T10:30:00Z",
                "before_request_id": "request-2",
                "limit": "25",
            },
        )
        audit = await client.get(
            "/admin/api/audit",
            params={
                "before_created_at": "2026-09-24T10:30:00Z",
                "before_event_id": "event-2",
                "action": "user_status_changed",
                "outcome": "succeeded",
                "target_user_id": "user-1",
                "limit": "25",
            },
        )

    assert activations.status_code == audit.status_code == 200
    activation_call = service.calls[0]
    audit_call = service.calls[1]
    assert activation_call[0] == "list_pending_activations"
    assert activation_call[1]["before_requested_at"] == _NOW
    assert activation_call[1]["before_request_id"] == "request-2"
    assert activation_call[1]["limit"] == 25
    assert audit_call[0] == "list_audit"
    assert audit_call[1]["before_created_at"] == _NOW
    assert audit_call[1]["before_event_id"] == "event-2"
    assert audit_call[1]["action"] is AdminAuditAction.USER_STATUS_CHANGED
    assert audit_call[1]["outcome"] is AdminAuditOutcome.SUCCEEDED
    assert audit_call[1]["target_user_id"] == "user-1"
    assert audit_call[1]["target_request_id"] is None


@pytest.mark.parametrize(("path", "body", "method_name"), _WRITE_CASES)
async def test_each_governed_write_returns_no_body_and_uses_server_context(
    path: str, body: dict[str, object], method_name: str
) -> None:
    service = _AdminIdentity()
    app = _app(service)
    async with _client(app, cookie=_LOCAL_COOKIE) as client:
        response = await client.post(
            path,
            content=json.dumps(body),
            headers=_headers(_LOCAL_COOKIE),
        )

    assert response.status_code == 204
    assert response.content == b""
    name, values = service.calls[-1]
    assert name == method_name
    assert values["actor"].user_id == LOCAL_ADMIN_USER_ID
    assert len(str(values["trace_id"])) == 32
    assert "tenant_id" not in values
    assert "environment_id" not in values
    assert "auth_source" not in values


@pytest.mark.parametrize(("path", "body", "_"), _WRITE_CASES)
async def test_each_governed_write_requires_origin_csrf_json_and_confirmation(
    path: str, body: dict[str, object], _: str
) -> None:
    service = _AdminIdentity()
    app = _app(service)
    without_confirmation = {**body, "confirm": False}
    with_integer_confirmation = {**body, "confirm": 1}
    with_float_confirmation = {**body, "confirm": 1.0}
    with_extra = {**body, "tenant_id": "other-tenant"}
    async with _client(app, cookie=_LOCAL_COOKIE) as client:
        responses = [
            await client.post(
                path,
                content=json.dumps(body),
                headers={
                    "content-type": "application/json",
                    "x-csrf-token": web_csrf_token(_LOCAL_COOKIE),
                },
            ),
            await client.post(
                path,
                content=json.dumps(body),
                headers={**_headers(_LOCAL_COOKIE), "origin": "https://evil.example.test"},
            ),
            await client.post(
                path,
                content=json.dumps(body),
                headers=[
                    ("origin", _ORIGIN),
                    ("origin", _ORIGIN),
                    ("content-type", "application/json"),
                    ("x-csrf-token", web_csrf_token(_LOCAL_COOKIE)),
                ],
            ),
            await client.post(
                path,
                content=json.dumps(body),
                headers=_headers(_LOCAL_COOKIE, csrf=False),
            ),
            await client.post(
                path,
                content=json.dumps(body),
                headers=[
                    ("origin", _ORIGIN),
                    ("content-type", "application/json"),
                    ("x-csrf-token", web_csrf_token(_LOCAL_COOKIE)),
                    ("x-csrf-token", web_csrf_token(_LOCAL_COOKIE)),
                ],
            ),
            await client.post(
                path,
                content=json.dumps(body),
                headers={
                    "origin": _ORIGIN,
                    "content-type": "text/plain",
                    "x-csrf-token": web_csrf_token(_LOCAL_COOKIE),
                },
            ),
            await client.post(
                path,
                content=json.dumps(without_confirmation),
                headers=_headers(_LOCAL_COOKIE),
            ),
            await client.post(
                path,
                content=json.dumps(with_integer_confirmation),
                headers=_headers(_LOCAL_COOKIE),
            ),
            await client.post(
                path,
                content=json.dumps(with_float_confirmation),
                headers=_headers(_LOCAL_COOKIE),
            ),
            await client.post(
                path,
                content=json.dumps(with_extra),
                headers=_headers(_LOCAL_COOKIE),
            ),
            await client.post(path, content=b"x" * 1025, headers=_headers(_LOCAL_COOKIE)),
        ]

    assert [response.status_code for response in responses] == [
        403,
        403,
        403,
        403,
        403,
        415,
        400,
        400,
        400,
        400,
        413,
    ]
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AdminIdentityNotFoundError(), 404),
        (AdminIdentityConflictError(), 409),
        (AdminIdentityUnavailableError(), 503),
    ],
)
async def test_application_failures_use_only_the_closed_http_error_family(
    error: Exception, status: int
) -> None:
    service = _AdminIdentity()
    service.error = error
    app = _app(service)
    async with _client(app, cookie=_LOCAL_COOKIE) as client:
        response = await client.get("/admin/api/users")

    assert response.status_code == status
    assert response.json() == {
        "error": {
            "code": {
                404: "not_found",
                409: "idempotency_conflict",
                503: "unavailable",
            }[status]
        }
    }
    assert str(error) not in response.text

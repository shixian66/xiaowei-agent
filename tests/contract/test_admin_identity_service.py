"""W3-lite Admin 身份应用边界。"""

import datetime as dt
from dataclasses import fields
from types import SimpleNamespace

import pytest

from xiaowei_agent.application.admin_identity import (
    AdminIdentityActor,
    AdminIdentityConflictError,
    AdminIdentityForbiddenError,
    AdminIdentityNotFoundError,
    AdminIdentityService,
    AdminIdentityUnavailableError,
)
from xiaowei_agent.contracts.activation import (
    ActivationRequest,
    ActivationStatus,
    PendingActivationPage,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditDenial,
    AdminAuditEffect,
    AdminAuditEvent,
    AdminAuditPage,
)
from xiaowei_agent.contracts.enums import (
    ActivationSource,
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    AdminCapability,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AdminUserPage,
    AdminUserRecord,
    UserAccount,
    UserRoleAssignment,
)
from xiaowei_agent.contracts.web_navigation import WebReturnIntent, WebReturnIntentKind
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryDecisionDeniedError,
    UserDirectoryNotFoundError,
)

NOW = dt.datetime(2026, 9, 24, tzinfo=dt.UTC)


class _Directory:
    def __init__(self) -> None:
        self.applied: list[object] = []
        self.error: Exception | None = None

    async def list_admin_users(self, *, query):
        if self.error is not None:
            raise self.error
        account = UserAccount(
            user_id="user-1",
            actor="user@example.test",
            display_name="User One",
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        return AdminUserPage(
            items=(
                AdminUserRecord(
                    account=account,
                    assignment=UserRoleAssignment(
                        user_id=account.user_id,
                        tenant_id=query.tenant_id,
                        environment_id=query.environment_id,
                        role=ProductRole.USER,
                        created_by="admin-1",
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                    feishu_bound=True,
                ),
            )
        )

    async def apply(self, *, command, context):
        if self.error is not None:
            raise self.error
        self.applied.append((command, context))
        return ()


class _Activations:
    async def list_pending(self, *, query):
        return PendingActivationPage(
            items=(
                ActivationRequest(
                    request_id="request-visible-only-as-hint",
                    tenant_id=query.tenant_id,
                    environment_id=query.environment_id,
                    provider=IdentitySource.FEISHU,
                    subject_ref="ou_private_subject",
                    subject_ref_digest="a" * 64,
                    source=ActivationSource.WEB_LOGIN,
                    return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
                    requested_at=NOW,
                    expires_at=NOW + dt.timedelta(hours=24),
                    status=ActivationStatus.PENDING,
                ),
            )
        )


class _Audit:
    def __init__(self) -> None:
        self.query = None
        self.denials: list[AdminAuditDenial] = []

    async def list_events(self, *, query):
        self.query = query
        return AdminAuditPage(
            items=(
                AdminAuditEvent(
                    event_id="event-1",
                    operation_id="operation-1",
                    tenant_id=query.tenant_id,
                    environment_id=query.environment_id,
                    actor_user_id="admin-1",
                    actor="admin@example.test",
                    auth_source=IdentitySource.LOCAL_ADMIN,
                    action=AdminAuditAction.USER_CREATED,
                    target_kind=AdminAuditTargetKind.USER,
                    target_ref_digest="b" * 64,
                    outcome=AdminAuditOutcome.SUCCEEDED,
                    effect=AdminAuditEffect(
                        role=ProductRole.USER, status=UserStatus.ACTIVE
                    ),
                    created_at=NOW,
                ),
            )
        )

    async def append_denied(self, *, denial):
        self.denials.append(denial)
        return denial


class _Decisions:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def decide(self, *, command, context):
        self.calls.append((command, context))
        return ()


def _actor(*capabilities: AdminCapability) -> AdminIdentityActor:
    return AdminIdentityActor(
        user_id="admin-1",
        tenant_id="tenant-a",
        environment_id="env-a",
        actor="admin@example.test",
        auth_source=IdentitySource.LOCAL_ADMIN,
        role=ProductRole.ADMIN,
        capabilities=frozenset(capabilities),
    )


def _service(directory: _Directory | None = None):
    directory = _Directory() if directory is None else directory
    activations = _Activations()
    audit = _Audit()
    decisions = _Decisions()
    return (
        AdminIdentityService(
            directory=directory,
            activations=activations,
            audit=audit,
            activation_decisions=decisions,
        ),
        SimpleNamespace(
            directory=directory,
            activations=activations,
            audit=audit,
            decisions=decisions,
        ),
    )


def test_actor_surface_does_not_carry_principal_or_subject() -> None:
    assert {field.name for field in fields(AdminIdentityActor)} == {
        "user_id",
        "tenant_id",
        "environment_id",
        "actor",
        "auth_source",
        "role",
        "capabilities",
    }


def test_service_exposes_only_the_seven_approved_operations() -> None:
    assert {
        name
        for name, value in vars(AdminIdentityService).items()
        if not name.startswith("_") and callable(value)
    } == {
        "list_users",
        "list_pending_activations",
        "list_audit",
        "set_user_status",
        "change_user_role",
        "approve_activation",
        "reject_activation",
    }


async def test_capabilities_are_checked_by_the_service() -> None:
    service, _ = _service()

    with pytest.raises(AdminIdentityForbiddenError):
        await service.list_users(actor=_actor(), after_actor=None, limit=50)
    with pytest.raises(AdminIdentityForbiddenError):
        await service.list_audit(
            actor=_actor(AdminCapability.MANAGE_USERS),
            before_created_at=None,
            before_event_id=None,
            action=None,
            outcome=None,
            target_user_id=None,
            target_request_id=None,
            limit=50,
        )


async def test_safe_pages_do_not_expose_subjects_or_digests() -> None:
    service, probes = _service()
    actor = _actor(AdminCapability.MANAGE_USERS, AdminCapability.VIEW_ADMIN_AUDIT)

    users = await service.list_users(actor=actor, after_actor=None, limit=50)
    pending = await service.list_pending_activations(
        actor=actor,
        before_requested_at=None,
        before_request_id=None,
        limit=50,
    )
    audit = await service.list_audit(
        actor=actor,
        before_created_at=None,
        before_event_id=None,
        action=None,
        outcome=None,
        target_user_id="user-1",
        target_request_id=None,
        limit=50,
    )

    rendered = (
        users.model_dump_json()
        + pending.model_dump_json()
        + audit.model_dump_json()
    )
    assert "ou_private_subject" not in rendered
    assert "a" * 64 not in rendered
    assert "b" * 64 not in rendered
    assert pending.items[0].subject_hint.startswith("申请 · ")
    assert "request-visible-only-as-hint" not in pending.items[0].subject_hint
    assert audit.next_event_id is None
    assert probes.audit.query.target_kind is AdminAuditTargetKind.USER
    assert probes.audit.query.target_ref_digest != "user-1"


async def test_writes_use_only_managed_commands_and_activation_decisions() -> None:
    service, probes = _service()
    actor = _actor(AdminCapability.MANAGE_USERS)

    await service.set_user_status(
        actor=actor,
        user_id="user-1",
        expected_status=UserStatus.ACTIVE,
        expected_role=ProductRole.USER,
        status=UserStatus.DISABLED,
        trace_id="0" * 32,
    )
    await service.change_user_role(
        actor=actor,
        user_id="user-1",
        expected_role=ProductRole.USER,
        role=ProductRole.OPERATOR,
        trace_id="1" * 32,
    )
    await service.approve_activation(
        actor=actor,
        request_id="request-1",
        account_actor="user@example.test",
        display_name="User One",
        approved_role=ProductRole.OPERATOR,
        trace_id="2" * 32,
    )
    await service.reject_activation(
        actor=actor, request_id="request-2", trace_id="3" * 32
    )

    assert [command.kind for command, _ in probes.directory.applied] == [
        "set_managed_user_status",
        "change_managed_user_role",
    ]
    assert [context.operation_id for _, context in probes.directory.applied] == [
        f"w3:{'0' * 32}",
        f"w3:{'1' * 32}",
    ]
    assert [command.kind for command, _ in probes.decisions.calls] == [
        "approve_activation",
        "reject_activation",
    ]
    assert [context.operation_id for _, context in probes.decisions.calls] == [
        f"w3:{'2' * 32}",
        f"w3:{'3' * 32}",
    ]


async def test_managed_denial_is_audited_without_exposing_the_target() -> None:
    directory = _Directory()
    directory.error = UserDirectoryDecisionDeniedError(
        AdminAuditReasonCode.ACTOR_NOT_ADMIN
    )
    service, probes = _service(directory)

    with pytest.raises(AdminIdentityForbiddenError):
        await service.change_user_role(
            actor=_actor(AdminCapability.MANAGE_USERS),
            user_id="user-private",
            expected_role=ProductRole.USER,
            role=ProductRole.OPERATOR,
            trace_id="4" * 32,
        )

    assert len(probes.audit.denials) == 1
    denial = probes.audit.denials[0]
    assert denial.action is AdminAuditAction.ROLE_ASSIGNED
    assert denial.reason_code is AdminAuditReasonCode.ACTOR_NOT_ADMIN
    assert denial.target_ref_digest != "user-private"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UserDirectoryNotFoundError("missing"), AdminIdentityNotFoundError),
        (UserDirectoryConflictError("stale"), AdminIdentityConflictError),
        (
            PersistenceUnavailableError(
                category=PersistenceUnavailableCategory.CONNECT
            ),
            AdminIdentityUnavailableError,
        ),
    ],
)
async def test_store_errors_are_mapped_to_the_closed_application_family(
    error: Exception, expected: type[Exception]
) -> None:
    directory = _Directory()
    directory.error = error
    service, _ = _service(directory)

    with pytest.raises(expected):
        await service.list_users(
            actor=_actor(AdminCapability.MANAGE_USERS),
            after_actor=None,
            limit=50,
        )


async def test_unwritable_managed_audit_is_a_closed_conflict() -> None:
    directory = _Directory()
    directory.error = AdminAuditUnwritableError()
    service, _ = _service(directory)

    with pytest.raises(AdminIdentityConflictError):
        await service.change_user_role(
            actor=_actor(AdminCapability.MANAGE_USERS),
            user_id="user-1",
            expected_role=ProductRole.USER,
            role=ProductRole.OPERATOR,
            trace_id="5" * 32,
        )

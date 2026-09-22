"""激活应用服务只编排存储边界，不复制目录授权判断。"""

import datetime as dt
from typing import cast

import pytest

from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.contracts import (
    ActivationSource,
    ActivationStatus,
    AdminAuditAction,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
)
from xiaowei_agent.contracts.activation import (
    ActivationRequest,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditDenial,
    AdminAuditEvent,
    AdminOperationContext,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.identity import (
    ApproveActivationCommand,
    DirectoryCommand,
    RejectActivationCommand,
)
from xiaowei_agent.persistence.identity import UserDirectoryDecisionDeniedError

_NOW = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)


def _request_command() -> CreateActivationCommand:
    return CreateActivationCommand(
        tenant_id="dev-local",
        environment_id="dev",
        provider=IdentitySource.FEISHU,
        subject_ref="ou_alice",
        source=ActivationSource.WEB_LOGIN,
    )


def _request() -> ActivationRequest:
    return ActivationRequest(
        request_id="activation-1",
        tenant_id="dev-local",
        environment_id="dev",
        provider=IdentitySource.FEISHU,
        subject_ref="ou_alice",
        subject_ref_digest="a" * 64,
        source=ActivationSource.WEB_LOGIN,
        requested_at=_NOW,
        expires_at=_NOW + dt.timedelta(hours=24),
        status=ActivationStatus.PENDING,
    )


def _context() -> AdminOperationContext:
    return AdminOperationContext(
        operation_id="approve-activation-1",
        actor_user_id="local-admin",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


class _ActivationStore:
    def __init__(self) -> None:
        self.commands: list[CreateActivationCommand] = []

    async def create_or_reuse(
        self, *, command: CreateActivationCommand
    ) -> ActivationRequest:
        self.commands.append(command)
        return _request()


class _Directory:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[DirectoryCommand, AdminOperationContext]] = []

    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        self.calls.append((command, context))
        if self.error is not None:
            raise self.error
        return (cast(AdminAuditEvent, object()),)


class _Audit:
    def __init__(self) -> None:
        self.denials: list[AdminAuditDenial] = []

    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent:
        self.denials.append(denial)
        return cast(AdminAuditEvent, object())


def _service(
    *, error: Exception | None = None
) -> tuple[IdentityActivationService, _ActivationStore, _Directory, _Audit]:
    activations = _ActivationStore()
    directory = _Directory(error)
    audit = _Audit()
    return (
        IdentityActivationService(
            activations=activations,
            directory=directory,
            audit=audit,
            tenant_id="dev-local",
            environment_id="dev",
        ),
        activations,
        directory,
        audit,
    )


@pytest.mark.asyncio
async def test_request_creation_is_a_single_activation_store_call() -> None:
    service, activations, directory, audit = _service()
    command = _request_command()

    created = await service.request_web(subject_ref=command.subject_ref)

    assert created.request_id == "activation-1"
    assert activations.commands == [command]
    assert directory.calls == []
    assert audit.denials == []


@pytest.mark.asyncio
async def test_group_request_derives_both_source_references() -> None:
    service, activations, _, _ = _service()

    await service.request_group(
        subject_ref="ou_alice", event_ref="event-1", chat_ref="chat-1"
    )

    assert len(activations.commands) == 1
    command = activations.commands[0]
    assert command.source is ActivationSource.FEISHU_GROUP
    assert command.source_event_ref == "event-1"
    assert command.source_chat_ref == "chat-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "action"),
    [
        (
            ApproveActivationCommand(
                request_id="activation-1",
                tenant_id="dev-local",
                environment_id="dev",
                actor="alice",
                display_name="Alice",
                approved_role=ProductRole.USER,
            ),
            AdminAuditAction.ACTIVATION_APPROVED,
        ),
        (
            RejectActivationCommand(
                request_id="activation-1",
                tenant_id="dev-local",
                environment_id="dev",
            ),
            AdminAuditAction.ACTIVATION_REJECTED,
        ),
    ],
)
async def test_decisions_delegate_once_to_the_atomic_directory_path(
    command: ApproveActivationCommand | RejectActivationCommand,
    action: AdminAuditAction,
) -> None:
    service, _, directory, audit = _service()
    context = _context()

    events = await service.decide(command=command, context=context)

    assert len(events) == 1
    assert directory.calls == [(command, context)]
    assert audit.denials == []
    assert action in {
        AdminAuditAction.ACTIVATION_APPROVED,
        AdminAuditAction.ACTIVATION_REJECTED,
    }


@pytest.mark.asyncio
async def test_store_denial_is_a_closed_audit_event_and_the_error_is_preserved() -> None:
    denied = UserDirectoryDecisionDeniedError(
        AdminAuditReasonCode.ACTOR_NOT_ADMIN
    )
    service, _, directory, audit = _service(error=denied)
    command = ApproveActivationCommand(
        request_id="activation-1",
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        display_name="Alice",
    )
    context = _context()

    with pytest.raises(UserDirectoryDecisionDeniedError) as caught:
        await service.decide(command=command, context=context)

    assert caught.value is denied
    assert directory.calls == [(command, context)]
    assert len(audit.denials) == 1
    denial = audit.denials[0]
    assert denial.operation_id == context.operation_id
    assert denial.action is AdminAuditAction.ACTIVATION_APPROVED
    assert denial.target_kind is AdminAuditTargetKind.ACTIVATION
    assert denial.target_ref_digest == admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.ACTIVATION,
        target_ref=command.request_id,
    )
    assert denial.reason_code is AdminAuditReasonCode.ACTOR_NOT_ADMIN
    assert not ({"outcome", "effect"} & set(type(denial).model_fields))

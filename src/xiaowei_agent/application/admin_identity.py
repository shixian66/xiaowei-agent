"""W3-lite Admin 身份查询与受管写的唯一应用边界。"""

import datetime as dt
import hashlib
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Never, TypeVar

from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.contracts.activation import PendingActivationListQuery
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditDenial,
    AdminAuditListQuery,
    AdminOperationContext,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.admin_identity import (
    AdminAuditSummary,
    AdminAuditViewPage,
    AdminPendingActivationPage,
    AdminPendingActivationSummary,
    AdminUserSummary,
    AdminUserViewPage,
)
from xiaowei_agent.contracts.enums import (
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
    AdminUserListQuery,
    ApproveActivationCommand,
    ChangeManagedUserRoleCommand,
    RejectActivationCommand,
    SetManagedUserStatusCommand,
)
from xiaowei_agent.persistence.activation import ActivationStore
from xiaowei_agent.persistence.admin_audit import (
    AdminAuditConflictError,
    AdminAuditStore,
)
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.identity import (
    UserDirectoryConflictError,
    UserDirectoryDecisionDeniedError,
    UserDirectoryNotFoundError,
    UserDirectoryStore,
)

_T = TypeVar("_T")
_SUBJECT_HINT_DOMAIN = "xiaowei.admin.activation_hint.v1"


class AdminIdentityError(RuntimeError):
    """不携带 Store/数据库正文的应用错误基类。"""


class AdminIdentityForbiddenError(AdminIdentityError):
    def __init__(self) -> None:
        super().__init__("admin identity access forbidden")


class AdminIdentityNotFoundError(AdminIdentityError):
    def __init__(self) -> None:
        super().__init__("admin identity target not found")


class AdminIdentityConflictError(AdminIdentityError):
    def __init__(self) -> None:
        super().__init__("admin identity state changed")


class AdminIdentityUnavailableError(AdminIdentityError):
    def __init__(self) -> None:
        super().__init__("admin identity service unavailable")


@dataclass(frozen=True)
class AdminIdentityActor:
    """管理服务需要的最小受信主体；不携带飞书 subject 或整个 principal。"""

    user_id: str
    tenant_id: str
    environment_id: str
    actor: str
    auth_source: IdentitySource
    role: ProductRole
    capabilities: frozenset[AdminCapability]


def _subject_hint(request_id: str) -> str:
    digest = hashlib.sha256(
        f"{_SUBJECT_HINT_DOMAIN}\x1f{request_id}".encode()
    ).hexdigest()
    return f"申请 · {digest[:8]}"


def _context(actor: AdminIdentityActor, trace_id: str) -> AdminOperationContext:
    return AdminOperationContext(
        operation_id=f"w3:{trace_id}",
        actor_user_id=actor.user_id,
        actor=actor.actor,
        auth_source=actor.auth_source,
    )


def _raise_closed(error: Exception) -> Never:
    if isinstance(error, UserDirectoryDecisionDeniedError):
        if error.reason_code in {
            AdminAuditReasonCode.TARGET_NOT_FOUND,
            AdminAuditReasonCode.SCOPE_MISMATCH,
        }:
            raise AdminIdentityNotFoundError from None
        raise AdminIdentityForbiddenError from None
    if isinstance(error, UserDirectoryNotFoundError):
        raise AdminIdentityNotFoundError from None
    if isinstance(error, (UserDirectoryConflictError, AdminAuditConflictError)):
        raise AdminIdentityConflictError from None
    if isinstance(error, (PersistenceUnavailableError, PersistenceIntegrityError)):
        raise AdminIdentityUnavailableError from None
    raise error


async def _closed(awaitable: Awaitable[_T]) -> _T:
    try:
        return await awaitable
    except Exception as error:
        _raise_closed(error)


class AdminIdentityService:
    """七个窄方法；不暴露通用命令执行或原始 Store 页面。"""

    def __init__(
        self,
        *,
        directory: UserDirectoryStore,
        activations: ActivationStore,
        audit: AdminAuditStore,
        activation_decisions: IdentityActivationService,
    ) -> None:
        self._directory = directory
        self._activations = activations
        self._audit = audit
        self._activation_decisions = activation_decisions

    @staticmethod
    def _require(actor: AdminIdentityActor, capability: AdminCapability) -> None:
        if actor.role is not ProductRole.ADMIN or capability not in actor.capabilities:
            raise AdminIdentityForbiddenError

    async def list_users(
        self, *, actor: AdminIdentityActor, after_actor: str | None, limit: int
    ) -> AdminUserViewPage:
        self._require(actor, AdminCapability.MANAGE_USERS)
        page = await _closed(
            self._directory.list_admin_users(
                query=AdminUserListQuery(
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                    after_actor=after_actor,
                    limit=limit,
                )
            )
        )
        return AdminUserViewPage(
            items=tuple(
                AdminUserSummary(
                    user_id=item.account.user_id,
                    actor=item.account.actor,
                    display_name=item.account.display_name,
                    status=item.account.status,
                    role=item.assignment.role,
                    feishu_bound=item.feishu_bound,
                    updated_at=max(item.account.updated_at, item.assignment.updated_at),
                )
                for item in page.items
            ),
            next_after_actor=page.next_after_actor,
        )

    async def list_pending_activations(
        self,
        *,
        actor: AdminIdentityActor,
        before_requested_at: dt.datetime | None,
        before_request_id: str | None,
        limit: int,
    ) -> AdminPendingActivationPage:
        self._require(actor, AdminCapability.MANAGE_USERS)
        page = await _closed(
            self._activations.list_pending(
                query=PendingActivationListQuery(
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                    before_requested_at=before_requested_at,
                    before_request_id=before_request_id,
                    limit=limit,
                )
            )
        )
        return AdminPendingActivationPage(
            items=tuple(
                AdminPendingActivationSummary(
                    request_id=item.request_id,
                    subject_hint=_subject_hint(item.request_id),
                    source=item.source,
                    requested_at=item.requested_at,
                    expires_at=item.expires_at,
                )
                for item in page.items
            ),
            next_requested_at=page.next_requested_at,
            next_request_id=page.next_request_id,
        )

    async def list_audit(
        self,
        *,
        actor: AdminIdentityActor,
        before_created_at: dt.datetime | None,
        before_event_id: str | None,
        action: AdminAuditAction | None,
        outcome: AdminAuditOutcome | None,
        target_user_id: str | None,
        target_request_id: str | None,
        limit: int,
    ) -> AdminAuditViewPage:
        self._require(actor, AdminCapability.VIEW_ADMIN_AUDIT)
        if target_user_id is not None and target_request_id is not None:
            raise AdminIdentityConflictError
        target_kind: AdminAuditTargetKind | None = None
        target_digest: str | None = None
        if target_user_id is not None:
            target_kind = AdminAuditTargetKind.USER
            target_digest = admin_audit_target_digest(
                target_kind=target_kind, target_ref=target_user_id
            )
        elif target_request_id is not None:
            target_kind = AdminAuditTargetKind.ACTIVATION
            target_digest = admin_audit_target_digest(
                target_kind=target_kind, target_ref=target_request_id
            )
        page = await _closed(
            self._audit.list_events(
                query=AdminAuditListQuery(
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                    before_created_at=before_created_at,
                    before_event_id=before_event_id,
                    action=action,
                    outcome=outcome,
                    target_kind=target_kind,
                    target_ref_digest=target_digest,
                    limit=limit,
                )
            )
        )
        return AdminAuditViewPage(
            items=tuple(
                AdminAuditSummary(
                    event_id=item.event_id,
                    actor=item.actor,
                    auth_source=item.auth_source,
                    action=item.action,
                    target_kind=item.target_kind,
                    outcome=item.outcome,
                    reason_code=item.reason_code,
                    effect=item.effect,
                    created_at=item.created_at,
                )
                for item in page.items
            ),
            next_created_at=page.next_created_at,
            next_event_id=page.next_event_id,
        )

    async def _apply_managed(
        self,
        *,
        actor: AdminIdentityActor,
        command: SetManagedUserStatusCommand | ChangeManagedUserRoleCommand,
        trace_id: str,
    ) -> None:
        self._require(actor, AdminCapability.MANAGE_USERS)
        context = _context(actor, trace_id)
        try:
            await self._directory.apply(command=command, context=context)
        except UserDirectoryDecisionDeniedError as error:
            try:
                await self._audit.append_denied(
                    denial=AdminAuditDenial(
                        operation_id=context.operation_id,
                        tenant_id=command.tenant_id,
                        environment_id=command.environment_id,
                        actor_user_id=context.actor_user_id,
                        actor=context.actor,
                        auth_source=context.auth_source,
                        action=(
                            AdminAuditAction.USER_STATUS_CHANGED
                            if isinstance(command, SetManagedUserStatusCommand)
                            else AdminAuditAction.ROLE_ASSIGNED
                        ),
                        target_kind=AdminAuditTargetKind.USER,
                        target_ref_digest=admin_audit_target_digest(
                            target_kind=AdminAuditTargetKind.USER,
                            target_ref=command.user_id,
                        ),
                        reason_code=error.reason_code,
                    )
                )
            except Exception as audit_error:
                _raise_closed(audit_error)
            _raise_closed(error)
        except Exception as error:
            _raise_closed(error)

    async def set_user_status(
        self,
        *,
        actor: AdminIdentityActor,
        user_id: str,
        expected_status: UserStatus,
        expected_role: Literal[ProductRole.USER, ProductRole.OPERATOR],
        status: UserStatus,
        trace_id: str,
    ) -> None:
        await self._apply_managed(
            actor=actor,
            command=SetManagedUserStatusCommand(
                user_id=user_id,
                tenant_id=actor.tenant_id,
                environment_id=actor.environment_id,
                expected_status=expected_status,
                expected_role=expected_role,
                status=status,
            ),
            trace_id=trace_id,
        )

    async def change_user_role(
        self,
        *,
        actor: AdminIdentityActor,
        user_id: str,
        expected_role: Literal[ProductRole.USER, ProductRole.OPERATOR],
        role: Literal[ProductRole.USER, ProductRole.OPERATOR],
        trace_id: str,
    ) -> None:
        await self._apply_managed(
            actor=actor,
            command=ChangeManagedUserRoleCommand(
                user_id=user_id,
                tenant_id=actor.tenant_id,
                environment_id=actor.environment_id,
                expected_role=expected_role,
                role=role,
            ),
            trace_id=trace_id,
        )

    async def approve_activation(
        self,
        *,
        actor: AdminIdentityActor,
        request_id: str,
        account_actor: str,
        display_name: str,
        approved_role: Literal[ProductRole.USER, ProductRole.OPERATOR],
        trace_id: str,
    ) -> None:
        self._require(actor, AdminCapability.MANAGE_USERS)
        await _closed(
            self._activation_decisions.decide(
                command=ApproveActivationCommand(
                    request_id=request_id,
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                    actor=account_actor,
                    display_name=display_name,
                    approved_role=approved_role,
                ),
                context=_context(actor, trace_id),
            )
        )

    async def reject_activation(
        self,
        *,
        actor: AdminIdentityActor,
        request_id: str,
        trace_id: str,
    ) -> None:
        self._require(actor, AdminCapability.MANAGE_USERS)
        await _closed(
            self._activation_decisions.decide(
                command=RejectActivationCommand(
                    request_id=request_id,
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                ),
                context=_context(actor, trace_id),
            )
        )


__all__ = [
    "AdminIdentityActor",
    "AdminIdentityConflictError",
    "AdminIdentityError",
    "AdminIdentityForbiddenError",
    "AdminIdentityNotFoundError",
    "AdminIdentityService",
    "AdminIdentityUnavailableError",
]

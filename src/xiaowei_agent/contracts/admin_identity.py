"""W3-lite Admin 管理服务到 Web 的安全投影契约。"""

from typing import Literal

from pydantic import Field, StrictBool

from xiaowei_agent.contracts.admin_audit import AdminAuditEffect
from xiaowei_agent.contracts.base import AwareDatetime, Contract, StrictStr
from xiaowei_agent.contracts.enums import (
    ActivationSource,
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import BoundedActor, BoundedId, BoundedName


class AdminUserSummary(Contract):
    user_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    status: UserStatus
    role: Literal[ProductRole.USER, ProductRole.OPERATOR, ProductRole.ADMIN]
    feishu_bound: StrictBool
    updated_at: AwareDatetime


class AdminPendingActivationSummary(Contract):
    request_id: BoundedId
    subject_hint: StrictStr = Field(pattern=r"^申请 · [0-9a-f]{8}$")
    source: ActivationSource
    requested_at: AwareDatetime
    expires_at: AwareDatetime


class AdminAuditSummary(Contract):
    event_id: BoundedId
    actor: BoundedActor
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    outcome: AdminAuditOutcome
    reason_code: AdminAuditReasonCode | None
    effect: AdminAuditEffect
    created_at: AwareDatetime


class AdminUserViewPage(Contract):
    items: tuple[AdminUserSummary, ...] = Field(max_length=100)
    next_after_actor: BoundedActor | None = None


class AdminPendingActivationPage(Contract):
    items: tuple[AdminPendingActivationSummary, ...] = Field(max_length=100)
    next_requested_at: AwareDatetime | None = None
    next_request_id: BoundedId | None = None


class AdminAuditViewPage(Contract):
    items: tuple[AdminAuditSummary, ...] = Field(max_length=100)
    next_created_at: AwareDatetime | None = None
    next_event_id: BoundedId | None = None


__all__ = [
    "AdminAuditSummary",
    "AdminAuditViewPage",
    "AdminPendingActivationPage",
    "AdminPendingActivationSummary",
    "AdminUserSummary",
    "AdminUserViewPage",
]

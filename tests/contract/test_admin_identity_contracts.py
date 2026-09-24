"""W3-lite Admin 列表契约的排序、游标与边界。"""

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import activation as activation_contracts
from xiaowei_agent.contracts import admin_audit as audit_contracts
from xiaowei_agent.contracts import identity as identity_contracts
from xiaowei_agent.contracts.activation import ActivationRequest
from xiaowei_agent.contracts.admin_audit import AdminAuditEffect, AdminAuditEvent
from xiaowei_agent.contracts.enums import (
    ActivationSource,
    ActivationStatus,
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
    WebReturnIntentKind,
)
from xiaowei_agent.contracts.identity import UserAccount, UserRoleAssignment
from xiaowei_agent.contracts.web_navigation import WebReturnIntent

_NOW = dt.datetime(2026, 9, 24, 9, 0, tzinfo=dt.UTC)


def _user(actor: str, *, user_id: str | None = None) -> Any:
    record_type = identity_contracts.AdminUserRecord  # type: ignore[attr-defined]
    resolved_user_id = user_id or f"u-{actor}"
    return record_type(
        account=UserAccount(
            user_id=resolved_user_id,
            actor=actor,
            display_name=actor.title(),
            status=UserStatus.ACTIVE,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        assignment=UserRoleAssignment(
            user_id=resolved_user_id,
            tenant_id="tenant-a",
            environment_id="dev",
            role=ProductRole.USER,
            created_by="admin-1",
            created_at=_NOW,
            updated_at=_NOW,
        ),
        feishu_bound=False,
    )


def _activation(request_id: str, requested_at: dt.datetime) -> ActivationRequest:
    return ActivationRequest(
        request_id=request_id,
        tenant_id="tenant-a",
        environment_id="dev",
        provider=IdentitySource.FEISHU,
        subject_ref="ou_subject",
        subject_ref_digest="a" * 64,
        source=ActivationSource.WEB_LOGIN,
        return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
        requested_at=requested_at,
        expires_at=requested_at + dt.timedelta(hours=24),
        status=ActivationStatus.PENDING,
    )


def _event(event_id: str, created_at: dt.datetime) -> AdminAuditEvent:
    return AdminAuditEvent(
        event_id=event_id,
        operation_id=f"op-{event_id}",
        tenant_id="tenant-a",
        environment_id="dev",
        actor_user_id="admin-1",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
        action=AdminAuditAction.USER_STATUS_CHANGED,
        target_kind=AdminAuditTargetKind.USER,
        target_ref_digest="b" * 64,
        outcome=AdminAuditOutcome.SUCCEEDED,
        effect=AdminAuditEffect(status=UserStatus.ACTIVE),
        created_at=created_at,
    )


@pytest.mark.parametrize("limit", (0, 101))
def test_admin_user_query_has_a_bounded_positive_limit(limit: int) -> None:
    query_type = identity_contracts.AdminUserListQuery  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        query_type(tenant_id="tenant-a", environment_id="dev", limit=limit)


def test_admin_user_page_is_strictly_ordered_and_cursor_matches_tail() -> None:
    page_type = identity_contracts.AdminUserPage  # type: ignore[attr-defined]
    page = page_type(
        items=(_user("alice"), _user("bob")),
        next_after_actor="bob",
    )
    assert [item.account.actor for item in page.items] == ["alice", "bob"]

    for items in (
        (_user("bob"), _user("alice")),
        (_user("alice", user_id="u-1"), _user("alice", user_id="u-2")),
    ):
        with pytest.raises(ValidationError):
            page_type(items=items, next_after_actor=items[-1].account.actor)
    with pytest.raises(ValidationError):
        page_type(items=(), next_after_actor="alice")
    with pytest.raises(ValidationError):
        page_type(items=(_user("alice"),), next_after_actor="bob")
    assert page_type(items=()).next_after_actor is None


@pytest.mark.parametrize("limit", (0, 101))
def test_pending_query_requires_a_bounded_limit(limit: int) -> None:
    query_type = activation_contracts.PendingActivationListQuery  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        query_type(tenant_id="tenant-a", environment_id="dev", limit=limit)


def test_pending_query_rejects_a_half_cursor() -> None:
    query_type = activation_contracts.PendingActivationListQuery  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        query_type(
            tenant_id="tenant-a",
            environment_id="dev",
            before_requested_at=_NOW,
        )
    with pytest.raises(ValidationError):
        query_type(
            tenant_id="tenant-a",
            environment_id="dev",
            before_request_id="request-1",
        )


def test_pending_page_is_strictly_descending_and_cursor_matches_tail() -> None:
    page_type = activation_contracts.PendingActivationPage  # type: ignore[attr-defined]
    newer = _activation("request-b", _NOW)
    older = _activation("request-a", _NOW - dt.timedelta(minutes=1))
    page = page_type(
        items=(newer, older),
        next_requested_at=older.requested_at,
        next_request_id=older.request_id,
    )
    assert page.items == (newer, older)

    for items in (
        (older, newer),
        (
            _activation("request-a", _NOW),
            _activation("request-a", _NOW),
        ),
    ):
        with pytest.raises(ValidationError):
            page_type(
                items=items,
                next_requested_at=items[-1].requested_at,
                next_request_id=items[-1].request_id,
            )
    with pytest.raises(ValidationError):
        page_type(items=(), next_requested_at=_NOW, next_request_id="request-a")
    with pytest.raises(ValidationError):
        page_type(
            items=(newer,),
            next_requested_at=older.requested_at,
            next_request_id=older.request_id,
        )
    assert page_type(items=()).next_request_id is None


@pytest.mark.parametrize("limit", (0, 101))
def test_audit_query_requires_a_bounded_limit(limit: int) -> None:
    query_type = audit_contracts.AdminAuditListQuery  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        query_type(tenant_id="tenant-a", environment_id="dev", limit=limit)


def test_audit_query_rejects_half_cursor_and_half_target_filter() -> None:
    query_type = audit_contracts.AdminAuditListQuery  # type: ignore[attr-defined]
    invalid = (
        {"before_created_at": _NOW},
        {"before_event_id": "event-1"},
        {"target_kind": AdminAuditTargetKind.USER},
        {"target_ref_digest": "c" * 64},
    )
    for fields in invalid:
        with pytest.raises(ValidationError):
            query_type(tenant_id="tenant-a", environment_id="dev", **fields)


def test_audit_page_is_strictly_descending_and_cursor_matches_tail() -> None:
    page_type = audit_contracts.AdminAuditPage  # type: ignore[attr-defined]
    newer = _event("event-b", _NOW)
    older = _event("event-a", _NOW - dt.timedelta(minutes=1))
    page = page_type(
        items=(newer, older),
        next_created_at=older.created_at,
        next_event_id=older.event_id,
    )
    assert page.items == (newer, older)

    for items in ((older, newer), (_event("event-a", _NOW), _event("event-a", _NOW))):
        with pytest.raises(ValidationError):
            page_type(
                items=items,
                next_created_at=items[-1].created_at,
                next_event_id=items[-1].event_id,
            )
    with pytest.raises(ValidationError):
        page_type(items=(), next_created_at=_NOW, next_event_id="event-a")
    with pytest.raises(ValidationError):
        page_type(items=(newer,), next_created_at=older.created_at, next_event_id=older.event_id)
    assert page_type(items=()).next_event_id is None

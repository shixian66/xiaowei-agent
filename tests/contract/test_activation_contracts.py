"""W1b 激活事实的契约边界。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts.activation import (
    ACTIVATION_SOURCE_INTENT_KINDS,
    ACTIVATION_SOURCE_REFERENCE_REQUIREMENTS,
    ACTIVATION_STATUS_DECISION_RULES,
    ActivationLookup,
    ActivationRequest,
    ActivationSource,
    ActivationStatus,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole
from xiaowei_agent.contracts.identity import (
    ApproveActivationCommand,
    RejectActivationCommand,
)
from xiaowei_agent.contracts.web_navigation import (
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.persistence.identity import derive_audit
from xiaowei_agent.persistence.rows import (
    activation_request_to_row,
    row_to_activation_request,
)

_NOW = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)
_LATER = _NOW + dt.timedelta(hours=24)


def _request(**updates: object) -> ActivationRequest:
    values: dict[str, object] = {
        "request_id": "activation-1",
        "tenant_id": "tenant-a",
        "environment_id": "env-a",
        "provider": IdentitySource.FEISHU,
        "subject_ref": "ou_subject",
        "subject_ref_digest": "a" * 64,
        "source": ActivationSource.WEB_LOGIN,
        "return_intent": WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
        "source_event_digest": None,
        "source_chat_digest": None,
        "requested_at": _NOW,
        "expires_at": _LATER,
        "status": ActivationStatus.PENDING,
        "decided_at": None,
        "decided_by": None,
        "approved_role": None,
    }
    values.update(updates)
    return ActivationRequest(**values)


def test_web_and_group_sources_have_closed_reference_shapes() -> None:
    assert _request().source is ActivationSource.WEB_LOGIN
    group = _request(
        source=ActivationSource.FEISHU_GROUP,
        return_intent=None,
        source_event_digest="b" * 64,
        source_chat_digest="c" * 64,
    )
    assert group.source is ActivationSource.FEISHU_GROUP

    with pytest.raises(ValidationError):
        _request(source_event_digest="b" * 64)
    with pytest.raises(ValidationError):
        _request(source=ActivationSource.FEISHU_GROUP, return_intent=None)


def test_activation_sources_have_total_reference_and_intent_policies() -> None:
    assert set(ACTIVATION_SOURCE_REFERENCE_REQUIREMENTS) == set(ActivationSource)
    assert set(ACTIVATION_SOURCE_INTENT_KINDS) == set(ActivationSource)
    assert ACTIVATION_SOURCE_INTENT_KINDS[ActivationSource.WEB_LOGIN] == {
        WebReturnIntentKind.WORKBENCH,
        WebReturnIntentKind.ADMIN_CENTER,
        WebReturnIntentKind.ACTIVATION_STATUS,
    }
    assert ACTIVATION_SOURCE_INTENT_KINDS[ActivationSource.SAFE_TASK_LINK] == {
        WebReturnIntentKind.SAFE_TASK_DETAIL
    }
    assert ACTIVATION_SOURCE_INTENT_KINDS[ActivationSource.FEISHU_GROUP] == set()


@pytest.mark.parametrize(
    ("source", "intent"),
    (
        (
            ActivationSource.WEB_LOGIN,
            WebReturnIntent(kind=WebReturnIntentKind.SAFE_TASK_DETAIL, task_id="task-1"),
        ),
        (
            ActivationSource.SAFE_TASK_LINK,
            WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
        ),
        (ActivationSource.FEISHU_GROUP, WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)),
    ),
)
def test_activation_source_and_return_intent_must_match(
    source: ActivationSource,
    intent: WebReturnIntent,
) -> None:
    event_digest = "b" * 64 if source is ActivationSource.FEISHU_GROUP else None
    chat_digest = "c" * 64 if source is ActivationSource.FEISHU_GROUP else None
    with pytest.raises(ValidationError):
        _request(
            source=source,
            return_intent=intent,
            source_event_digest=event_digest,
            source_chat_digest=chat_digest,
        )


def test_safe_task_link_has_no_group_reference_digests() -> None:
    intent = WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="task-1",
    )
    assert (
        _request(source=ActivationSource.SAFE_TASK_LINK, return_intent=intent).source
        is ActivationSource.SAFE_TASK_LINK
    )
    with pytest.raises(ValidationError):
        _request(
            source=ActivationSource.SAFE_TASK_LINK,
            return_intent=intent,
            source_event_digest="b" * 64,
        )


def test_activation_status_policy_is_total_over_the_status_enum() -> None:
    assert set(ACTIVATION_STATUS_DECISION_RULES) == set(ActivationStatus)


@pytest.mark.parametrize(
    ("status", "decided_at", "decided_by", "approved_role"),
    (
        (ActivationStatus.PENDING, None, None, None),
        (ActivationStatus.EXPIRED, None, None, None),
        (ActivationStatus.REJECTED, _LATER, "admin-1", None),
        (ActivationStatus.APPROVED, _LATER, "admin-1", ProductRole.USER),
        (ActivationStatus.APPROVED, _LATER, "admin-1", ProductRole.OPERATOR),
    ),
)
def test_status_fields_form_a_closed_state_machine(
    status: ActivationStatus,
    decided_at: dt.datetime | None,
    decided_by: str | None,
    approved_role: ProductRole | None,
) -> None:
    assert (
        _request(
            status=status,
            decided_at=decided_at,
            decided_by=decided_by,
            approved_role=approved_role,
        ).status
        is status
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"status": ActivationStatus.PENDING, "decided_at": _LATER},
        {"status": ActivationStatus.REJECTED, "decided_at": _LATER},
        {
            "status": ActivationStatus.REJECTED,
            "decided_at": _LATER,
            "decided_by": "admin-1",
            "approved_role": ProductRole.USER,
        },
        {
            "status": ActivationStatus.APPROVED,
            "decided_at": _LATER,
            "decided_by": "admin-1",
        },
        {
            "status": ActivationStatus.APPROVED,
            "decided_at": _LATER,
            "decided_by": "admin-1",
            "approved_role": ProductRole.ADMIN,
        },
        {"status": ActivationStatus.EXPIRED, "decided_by": "admin-1"},
    ),
)
def test_invalid_state_combinations_are_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _request(**updates)


def test_expiration_must_be_after_request_time() -> None:
    with pytest.raises(ValidationError):
        _request(expires_at=_NOW)


def test_raw_subject_and_source_references_are_not_dumped_or_represented() -> None:
    command = CreateActivationCommand(
        tenant_id="tenant-a",
        environment_id="env-a",
        provider=IdentitySource.FEISHU,
        subject_ref="ou_subject",
        source=ActivationSource.FEISHU_GROUP,
        return_intent=None,
        source_event_ref="event-1",
        source_chat_ref="chat-1",
    )
    request = _request()

    dumped = command.model_dump()
    assert "subject_ref" not in dumped
    assert "source_event_ref" not in dumped
    assert "source_chat_ref" not in dumped
    assert "ou_subject" not in repr(command)
    assert "ou_subject" not in repr(request)
    assert "subject_ref" not in request.model_dump()


def test_lookup_requires_request_id_and_explicit_scope() -> None:
    lookup = ActivationLookup(
        request_id="activation-1",
        tenant_id="tenant-a",
        environment_id="env-a",
    )
    assert lookup.request_id == "activation-1"

    with pytest.raises(ValidationError):
        ActivationLookup.model_validate(
            {"request_id": "activation-1", "tenant_id": "tenant-a"}
        )


def test_activation_request_row_round_trip_preserves_closed_types() -> None:
    request = _request(
        source=ActivationSource.FEISHU_GROUP,
        return_intent=None,
        source_event_digest="b" * 64,
        source_chat_digest="c" * 64,
    )

    row = activation_request_to_row(request)

    assert row["provider"] == IdentitySource.FEISHU.value
    assert row["source"] == ActivationSource.FEISHU_GROUP.value
    assert row["status"] == ActivationStatus.PENDING.value
    assert row_to_activation_request(row) == request


def test_activation_decision_commands_are_narrow_and_never_grant_admin() -> None:
    approved = ApproveActivationCommand(
        request_id="activation-1",
        tenant_id="tenant-a",
        environment_id="env-a",
        actor="new-user",
        display_name="New User",
        approved_role=ProductRole.USER,
    )
    rejected = RejectActivationCommand(
        request_id="activation-1",
        tenant_id="tenant-a",
        environment_id="env-a",
    )
    assert approved.approved_role is ProductRole.USER
    assert rejected.request_id == approved.request_id
    with pytest.raises(ValidationError):
        ApproveActivationCommand(
            request_id="activation-1",
            tenant_id="tenant-a",
            environment_id="env-a",
            actor="new-user",
            display_name="New User",
            approved_role=ProductRole.ADMIN,
        )


@pytest.mark.parametrize(
    ("field", "accepted", "rejected"),
    (
        ("actor", "a" * 256, "a" * 257),
        ("display_name", "n" * 128, "n" * 129),
    ),
)
def test_activation_account_facts_have_explicit_two_sided_bounds(
    field: str, accepted: str, rejected: str
) -> None:
    values = {
        "request_id": "activation-1",
        "tenant_id": "tenant-a",
        "environment_id": "env-a",
        "actor": "new-user",
        "display_name": "New User",
    }
    values[field] = accepted
    assert ApproveActivationCommand(**values).model_dump()[field] == accepted

    values[field] = rejected
    with pytest.raises(ValidationError):
        ApproveActivationCommand(**values)


def test_activation_audit_target_and_effect_are_derived_from_the_command() -> None:
    context = AdminOperationContext(
        operation_id="approve-1",
        actor_user_id="admin-1",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
    )
    approved = ApproveActivationCommand(
        request_id="activation-1",
        tenant_id="tenant-a",
        environment_id="env-a",
        actor="new-user",
        display_name="New User",
        approved_role=ProductRole.OPERATOR,
    )

    event = derive_audit(approved, context, target_ref="ignored-user-target")

    assert event.action.value == "activation_approved"
    assert event.target_kind.value == "activation"
    assert event.effect.role is ProductRole.OPERATOR
    assert event.effect.status.value == "active"

    denied = derive_audit(
        RejectActivationCommand(
            request_id="activation-1",
            tenant_id="tenant-a",
            environment_id="env-a",
        ),
        context,
        target_ref="ignored-user-target",
    )
    assert denied.action.value == "activation_rejected"
    assert denied.target_kind.value == "activation"
    assert denied.effect.is_empty

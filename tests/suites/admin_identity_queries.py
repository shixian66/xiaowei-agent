"""Admin 身份三类查询的内存/PostgreSQL 共享行为套件。"""

from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

from xiaowei_agent.contracts.activation import (
    ActivationSource,
    ActivationStatus,
    CreateActivationCommand,
    PendingActivationListQuery,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditListQuery,
    AdminOperationContext,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AdminUserListQuery,
    BindExternalIdentityCommand,
    CreateUserCommand,
    SetUserStatusCommand,
)
from xiaowei_agent.contracts.web_navigation import WebReturnIntent, WebReturnIntentKind

TENANT = "tenant-a"
ENVIRONMENT = "env-a"


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    for case in cases:
        namespace[case.__name__] = case


def _context(index: int) -> AdminOperationContext:
    return AdminOperationContext(
        operation_id=f"admin-query-{index}",
        actor_user_id="admin-1",
        actor="admin@example.test",
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


async def _create_user(
    bundle: Any,
    *,
    index: int,
    actor: str,
    scope: tuple[str, str] = (TENANT, ENVIRONMENT),
    bind_feishu: bool = False,
    disabled: bool = False,
) -> None:
    tenant_id, environment_id = scope
    user_id = f"user-{index}"
    await bundle.directory.apply(
        command=CreateUserCommand(
            user_id=user_id,
            actor=actor,
            display_name=f"User {index}",
            tenant_id=tenant_id,
            environment_id=environment_id,
            role=ProductRole.USER,
        ),
        context=_context(index * 10),
    )
    if bind_feishu:
        await bundle.directory.apply(
            command=BindExternalIdentityCommand(
                user_id=user_id,
                tenant_id=tenant_id,
                environment_id=environment_id,
                subject_ref=f"ou_subject_{index}",
            ),
            context=_context(index * 10 + 1),
        )
    if disabled:
        await bundle.directory.apply(
            command=SetUserStatusCommand(
                user_id=user_id,
                tenant_id=tenant_id,
                environment_id=environment_id,
                status=UserStatus.DISABLED,
            ),
            context=_context(index * 10 + 2),
        )


def _activation(subject_ref: str, *, environment_id: str = ENVIRONMENT) -> CreateActivationCommand:
    return CreateActivationCommand(
        tenant_id=TENANT,
        environment_id=environment_id,
        provider=IdentitySource.FEISHU,
        subject_ref=subject_ref,
        source=ActivationSource.WEB_LOGIN,
        return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
    )


async def test_admin_users_are_scoped_batched_and_keyset_paginated(
    admin_query_bundle: Any,
) -> None:
    await _create_user(admin_query_bundle, index=1, actor="adam@example.test")
    await _create_user(
        admin_query_bundle,
        index=2,
        actor="bravo@example.test",
        bind_feishu=True,
    )
    await _create_user(
        admin_query_bundle,
        index=3,
        actor="charlie@example.test",
        disabled=True,
    )
    await _create_user(
        admin_query_bundle,
        index=4,
        actor="other@example.test",
        scope=(TENANT, "other-env"),
    )

    first = await admin_query_bundle.directory.list_admin_users(
        query=AdminUserListQuery(
            tenant_id=TENANT, environment_id=ENVIRONMENT, limit=2
        )
    )
    assert [item.account.actor for item in first.items] == [
        "adam@example.test",
        "bravo@example.test",
    ]
    assert [item.feishu_bound for item in first.items] == [False, True]
    assert first.next_after_actor == "bravo@example.test"

    second = await admin_query_bundle.directory.list_admin_users(
        query=AdminUserListQuery(
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            after_actor=first.next_after_actor,
            limit=2,
        )
    )
    assert [item.account.actor for item in second.items] == [
        "charlie@example.test"
    ]
    assert second.items[0].account.status is UserStatus.DISABLED
    assert second.next_after_actor is None


async def test_pending_activations_exclude_expired_and_use_a_stable_tie_break(
    admin_query_bundle: Any,
) -> None:
    expired = await admin_query_bundle.activations.create_or_reuse(
        command=_activation("ou_expired")
    )
    admin_query_bundle.clock.advance(seconds=24 * 60 * 60)
    one = await admin_query_bundle.activations.create_or_reuse(
        command=_activation("ou_one")
    )
    two = await admin_query_bundle.activations.create_or_reuse(
        command=_activation("ou_two")
    )
    await admin_query_bundle.activations.create_or_reuse(
        command=_activation("ou_other", environment_id="other-env")
    )

    first = await admin_query_bundle.activations.list_pending(
        query=PendingActivationListQuery(
            tenant_id=TENANT, environment_id=ENVIRONMENT, limit=1
        )
    )
    expected = sorted(
        (one, two),
        key=lambda item: (item.requested_at, item.request_id),
        reverse=True,
    )
    assert list(first.items) == expected[:1]
    assert (first.next_requested_at, first.next_request_id) == (
        expected[0].requested_at,
        expected[0].request_id,
    )

    second = await admin_query_bundle.activations.list_pending(
        query=PendingActivationListQuery(
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            before_requested_at=first.next_requested_at,
            before_request_id=first.next_request_id,
            limit=1,
        )
    )
    assert list(second.items) == expected[1:]
    assert second.next_requested_at is None
    stored_expired = await admin_query_bundle.activations.load(
        query=type(admin_query_bundle).activation_lookup(expired.request_id)
    )
    assert stored_expired is not None
    assert stored_expired.status is ActivationStatus.EXPIRED


async def test_audit_events_are_scoped_filtered_and_keyset_paginated(
    admin_query_bundle: Any,
) -> None:
    await _create_user(admin_query_bundle, index=11, actor="one@example.test")
    await _create_user(admin_query_bundle, index=12, actor="two@example.test")
    await _create_user(
        admin_query_bundle,
        index=13,
        actor="other@example.test",
        scope=(TENANT, "other-env"),
    )

    first = await admin_query_bundle.audit.list_events(
        query=AdminAuditListQuery(
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            action=AdminAuditAction.USER_CREATED,
            outcome=AdminAuditOutcome.SUCCEEDED,
            limit=1,
        )
    )
    assert len(first.items) == 1
    assert first.items[0].environment_id == ENVIRONMENT
    assert first.next_created_at is not None

    second = await admin_query_bundle.audit.list_events(
        query=AdminAuditListQuery(
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            before_created_at=first.next_created_at,
            before_event_id=first.next_event_id,
            action=AdminAuditAction.USER_CREATED,
            outcome=AdminAuditOutcome.SUCCEEDED,
            limit=1,
        )
    )
    assert len(second.items) == 1
    assert second.items[0].event_id != first.items[0].event_id
    assert second.next_created_at is None


ADMIN_IDENTITY_QUERY_CASES = (
    test_admin_users_are_scoped_batched_and_keyset_paginated,
    test_pending_activations_exclude_expired_and_use_a_stable_tie_break,
    test_audit_events_are_scoped_filtered_and_keyset_paginated,
)

ALL_GROUPS = {"admin_identity_queries": ADMIN_IDENTITY_QUERY_CASES}

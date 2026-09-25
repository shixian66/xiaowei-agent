"""``ActivationStore`` 的内存与 PostgreSQL 共享行为套件。"""

import datetime as dt
from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest

from xiaowei_agent.contracts.activation import (
    ActivationLookup,
    ActivationRequest,
    ActivationSource,
    ActivationStatus,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole
from xiaowei_agent.contracts.web_navigation import (
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.persistence.activation import (
    ActivationCapacityError,
    activation_subject_digest,
)

TENANT = "tenant-a"
ENVIRONMENT = "env-a"


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    for case in cases:
        namespace[case.__name__] = case


def command(
    subject_ref: str = "ou_subject",
    *,
    source: ActivationSource = ActivationSource.WEB_LOGIN,
) -> CreateActivationCommand:
    intent = (
        None
        if source is ActivationSource.FEISHU_GROUP
        else WebReturnIntent(
            kind=(
                WebReturnIntentKind.SAFE_TASK_DETAIL
                if source is ActivationSource.SAFE_TASK_LINK
                else WebReturnIntentKind.WORKBENCH
            ),
            task_id="task-1" if source is ActivationSource.SAFE_TASK_LINK else None,
        )
    )
    return CreateActivationCommand(
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        provider=IdentitySource.FEISHU,
        subject_ref=subject_ref,
        source=source,
        return_intent=intent,
        source_event_ref="event-1" if source is ActivationSource.FEISHU_GROUP else None,
        source_chat_ref="chat-1" if source is ActivationSource.FEISHU_GROUP else None,
    )


def lookup(request_id: str, *, environment_id: str = ENVIRONMENT) -> ActivationLookup:
    return ActivationLookup(
        request_id=request_id,
        tenant_id=TENANT,
        environment_id=environment_id,
    )


async def test_create_is_idempotent_for_the_same_active_subject(activation_store: Any) -> None:
    first = await activation_store.create_or_reuse(command=command())
    second = await activation_store.create_or_reuse(command=command())

    assert first == second
    assert first.status is ActivationStatus.PENDING
    assert first.expires_at > first.requested_at


async def test_lookup_requires_the_matching_scope(activation_store: Any) -> None:
    created = await activation_store.create_or_reuse(command=command())

    assert await activation_store.load(query=lookup(created.request_id)) == created
    assert (
        await activation_store.load(
            query=lookup(created.request_id, environment_id="other-env")
        )
        is None
    )


async def test_expired_pending_is_harvested_before_a_new_request_is_created(
    activation_store: Any, clock: Any
) -> None:
    first = await activation_store.create_or_reuse(command=command())
    clock.advance(seconds=24 * 60 * 60)

    second = await activation_store.create_or_reuse(command=command())

    assert second.request_id != first.request_id
    assert second.status is ActivationStatus.PENDING
    expired = await activation_store.load(query=lookup(first.request_id))
    assert expired is not None
    assert expired.status is ActivationStatus.EXPIRED
    assert expired.decided_at is None
    assert expired.decided_by is None


async def test_group_references_are_replaced_by_domain_separated_digests(
    activation_store: Any,
) -> None:
    created = await activation_store.create_or_reuse(
        command=command(source=ActivationSource.FEISHU_GROUP)
    )

    assert created.source_event_digest is not None
    assert created.source_chat_digest is not None
    assert created.source_event_digest != created.source_chat_digest
    rendered = repr(created) + created.model_dump_json()
    assert "event-1" not in rendered
    assert "chat-1" not in rendered
    assert "ou_subject" not in rendered


async def test_safe_task_link_persists_only_the_closed_task_intent(
    activation_store: Any,
) -> None:
    created = await activation_store.create_or_reuse(
        command=command(source=ActivationSource.SAFE_TASK_LINK)
    )

    assert created.source is ActivationSource.SAFE_TASK_LINK
    assert created.return_intent == WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="task-1",
    )
    assert created.source_event_digest is None
    assert created.source_chat_digest is None


async def test_pending_subject_reuse_preserves_the_original_return_intent(
    activation_store: Any,
) -> None:
    original = await activation_store.create_or_reuse(command=command())
    reused = await activation_store.create_or_reuse(
        command=command(source=ActivationSource.SAFE_TASK_LINK)
    )

    assert reused == original
    assert reused.source is ActivationSource.WEB_LOGIN
    assert reused.return_intent == WebReturnIntent(
        kind=WebReturnIntentKind.WORKBENCH
    )


async def test_capacity_is_checked_before_subject_reuse(
    activation_store: Any,
) -> None:
    for index in range(1024):
        await activation_store.create_or_reuse(command=command(f"ou_{index}"))

    with pytest.raises(ActivationCapacityError):
        await activation_store.create_or_reuse(command=command("ou_0"))
    with pytest.raises(ActivationCapacityError):
        await activation_store.create_or_reuse(command=command("ou_new"))


ACTIVATION_STORE_CASES = (
    test_create_is_idempotent_for_the_same_active_subject,
    test_lookup_requires_the_matching_scope,
    test_expired_pending_is_harvested_before_a_new_request_is_created,
    test_group_references_are_replaced_by_domain_separated_digests,
    test_safe_task_link_persists_only_the_closed_task_intent,
    test_pending_subject_reuse_preserves_the_original_return_intent,
    test_capacity_is_checked_before_subject_reuse,
)

# --- 终态保留（W5 §2.1） ------------------------------------------------------
#
# 绑定方额外提供两个 fixture：``retention_store``（被测 ``ActivationRetentionStore``）与
# ``seed_activation``（把一条已构造好的 ``ActivationRequest`` 原样写入同一份事实）。
# 终态行只能由目录写路径产生，套件直接落事实，免得为造数引入整条批准链。

RETENTION = dt.timedelta(days=30)
OTHER_TENANT = "tenant-b"
OTHER_ENVIRONMENT = "env-b"


def stored_request(
    request_id: str,
    *,
    status: ActivationStatus,
    terminal_at: dt.datetime,
    tenant_id: str = TENANT,
    environment_id: str = ENVIRONMENT,
) -> ActivationRequest:
    """造一条处于 ``status`` 且终态时间为 ``terminal_at`` 的申请。

    PENDING/EXPIRED 的"终态时间"是 ``expires_at``；APPROVED/REJECTED 是 ``decided_at``。
    """
    requested_at = terminal_at - dt.timedelta(hours=1)
    expires_at = (
        terminal_at
        if status in (ActivationStatus.PENDING, ActivationStatus.EXPIRED)
        else requested_at + dt.timedelta(hours=24)
    )
    decided = status in (ActivationStatus.APPROVED, ActivationStatus.REJECTED)
    subject = f"ou_{request_id}"
    return ActivationRequest(
        request_id=request_id,
        tenant_id=tenant_id,
        environment_id=environment_id,
        provider=IdentitySource.FEISHU,
        subject_ref=subject,
        subject_ref_digest=activation_subject_digest(
            CreateActivationCommand(
                tenant_id=tenant_id,
                environment_id=environment_id,
                provider=IdentitySource.FEISHU,
                subject_ref=subject,
                source=ActivationSource.WEB_LOGIN,
                return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
            )
        ),
        source=ActivationSource.WEB_LOGIN,
        return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
        requested_at=requested_at,
        expires_at=expires_at,
        status=status,
        decided_at=terminal_at if decided else None,
        decided_by="admin-1" if decided else None,
        approved_role=(
            ProductRole.USER if status is ActivationStatus.APPROVED else None
        ),
    )


async def _remaining(activation_store: Any, request: ActivationRequest) -> Any:
    return await activation_store.load(
        query=ActivationLookup(
            request_id=request.request_id,
            tenant_id=request.tenant_id,
            environment_id=request.environment_id,
        )
    )


async def test_retention_deletes_terminal_rows_at_or_before_the_fixed_boundary(
    retention_store: Any, seed_activation: Any, activation_store: Any, clock: Any
) -> None:
    cutoff = clock() - RETENTION
    second = dt.timedelta(seconds=1)
    rows = {}
    for status in (
        ActivationStatus.APPROVED,
        ActivationStatus.REJECTED,
        ActivationStatus.EXPIRED,
    ):
        for label, terminal_at in (
            ("older", cutoff - second),
            ("exact", cutoff),
            ("inside", cutoff + second),
        ):
            request = stored_request(
                f"{status.value}-{label}", status=status, terminal_at=terminal_at
            )
            rows[(status, label)] = request
            await seed_activation(request)

    report = await retention_store.purge_expired_terminal()

    assert report.model_dump() == {
        "pending_expired": 0,
        "approved_deleted": 2,
        "rejected_deleted": 2,
        "expired_deleted": 2,
    }
    for (_, label), request in rows.items():
        remaining = await _remaining(activation_store, request)
        if label == "inside":
            assert remaining == request
        else:
            assert remaining is None


async def test_retention_expires_pending_first_and_never_deletes_live_pending(
    retention_store: Any, seed_activation: Any, activation_store: Any, clock: Any
) -> None:
    now = clock()
    live = stored_request(
        "pending-live",
        status=ActivationStatus.PENDING,
        terminal_at=now + dt.timedelta(hours=23),
    )
    recently_lapsed = stored_request(
        "pending-lapsed",
        status=ActivationStatus.PENDING,
        terminal_at=now - dt.timedelta(hours=1),
    )
    long_lapsed = stored_request(
        "pending-long-lapsed",
        status=ActivationStatus.PENDING,
        terminal_at=now - RETENTION - dt.timedelta(days=1),
    )
    for request in (live, recently_lapsed, long_lapsed):
        await seed_activation(request)

    report = await retention_store.purge_expired_terminal()

    assert report.model_dump() == {
        "pending_expired": 2,
        "approved_deleted": 0,
        "rejected_deleted": 0,
        "expired_deleted": 1,
    }
    assert await _remaining(activation_store, live) == live
    lapsed = await _remaining(activation_store, recently_lapsed)
    assert lapsed is not None
    assert lapsed.status is ActivationStatus.EXPIRED
    assert lapsed.decided_at is None
    assert await _remaining(activation_store, long_lapsed) is None


async def test_retention_is_idempotent(
    retention_store: Any, seed_activation: Any, clock: Any
) -> None:
    await seed_activation(
        stored_request(
            "rejected-old",
            status=ActivationStatus.REJECTED,
            terminal_at=clock() - RETENTION - dt.timedelta(days=2),
        )
    )

    first = await retention_store.purge_expired_terminal()
    second = await retention_store.purge_expired_terminal()

    assert first.rejected_deleted == 1
    assert second.model_dump() == {
        "pending_expired": 0,
        "approved_deleted": 0,
        "rejected_deleted": 0,
        "expired_deleted": 0,
    }


async def test_retention_covers_every_scope_in_one_run(
    retention_store: Any, seed_activation: Any, activation_store: Any, clock: Any
) -> None:
    old = clock() - RETENTION - dt.timedelta(days=1)
    fresh = clock() - dt.timedelta(days=1)
    kept = []
    for tenant_id, environment_id in (
        (TENANT, ENVIRONMENT),
        (OTHER_TENANT, OTHER_ENVIRONMENT),
    ):
        await seed_activation(
            stored_request(
                f"approved-old-{tenant_id}",
                status=ActivationStatus.APPROVED,
                terminal_at=old,
                tenant_id=tenant_id,
                environment_id=environment_id,
            )
        )
        request = stored_request(
            f"approved-fresh-{tenant_id}",
            status=ActivationStatus.APPROVED,
            terminal_at=fresh,
            tenant_id=tenant_id,
            environment_id=environment_id,
        )
        kept.append(request)
        await seed_activation(request)

    report = await retention_store.purge_expired_terminal()

    assert report.approved_deleted == 2
    for request in kept:
        assert await _remaining(activation_store, request) == request


async def test_retention_report_carries_counts_only(
    retention_store: Any, seed_activation: Any, clock: Any
) -> None:
    await seed_activation(
        stored_request(
            "expired-old",
            status=ActivationStatus.EXPIRED,
            terminal_at=clock() - RETENTION - dt.timedelta(days=3),
        )
    )

    report = await retention_store.purge_expired_terminal()

    rendered = repr(report) + report.model_dump_json()
    assert "expired-old" not in rendered
    assert "ou_" not in rendered
    assert TENANT not in rendered
    assert ENVIRONMENT not in rendered


ACTIVATION_RETENTION_CASES = (
    test_retention_deletes_terminal_rows_at_or_before_the_fixed_boundary,
    test_retention_expires_pending_first_and_never_deletes_live_pending,
    test_retention_is_idempotent,
    test_retention_covers_every_scope_in_one_run,
    test_retention_report_carries_counts_only,
)

ALL_GROUPS = {
    "activation_store": ACTIVATION_STORE_CASES,
    "activation_retention": ACTIVATION_RETENTION_CASES,
}

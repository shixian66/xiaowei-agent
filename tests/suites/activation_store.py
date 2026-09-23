"""``ActivationStore`` 的内存与 PostgreSQL 共享行为套件。"""

from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest

from xiaowei_agent.contracts.activation import (
    ActivationLookup,
    ActivationSource,
    ActivationStatus,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.enums import IdentitySource
from xiaowei_agent.contracts.web_navigation import (
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.persistence.activation import ActivationCapacityError

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

ALL_GROUPS = {"activation_store": ACTIVATION_STORE_CASES}

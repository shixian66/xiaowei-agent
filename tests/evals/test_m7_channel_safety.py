"""M7 L0：渠道身份、读取 ACL、展示输入与投影 fencing 安全矩阵。"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import lookup_for, make_submission

from xiaowei_agent.application.channel_access import (
    TaskAccessNotFoundError,
    TaskAccessQuery,
    TaskAccessService,
)
from xiaowei_agent.application.channel_projection import (
    ChannelMessageError,
    ChannelProjectionService,
)
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    DestinationKind,
    FeishuProjectionInput,
    IdentitySource,
    ProjectionErrorCode,
    ProjectionState,
    RenderPayload,
    RenderSection,
    TaskRecord,
    TaskStatus,
    TaskView,
    content_digest,
    task_query_path,
)
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.feishu_listener import FeishuListener
from xiaowei_agent.interfaces.feishu_sdk import FeishuMessageEvent
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    ClaimProjectionCommand,
    CreateProjectionSubscriptionCommand,
    RecordInitialProjectionCommand,
)
from xiaowei_agent.persistence.evidence import InMemoryEvidenceLedger
from xiaowei_agent.persistence.fake import InMemoryChannelStore
from xiaowei_agent.persistence.plans import InMemoryPlanStore
from xiaowei_agent.rendering.feishu import render_feishu_card

pytestmark = pytest.mark.security

_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "m7_channel_safety.json").read_text(
        encoding="utf-8"
    )
)
_CASES: list[dict[str, Any]] = _CORPUS["cases"]
_REQUESTED_CARRIERS: set[str] = set()


def _by(carrier: str) -> list[dict[str, Any]]:
    matches = [case for case in _CASES if case["carrier"] == carrier]
    if not matches:
        raise AssertionError(f"M7 safety carrier has no corpus cases: {carrier}")
    _REQUESTED_CARRIERS.add(carrier)
    return matches


def _principal(
    *,
    actor: str,
    subject_ref: str,
    environment_id: str = "dev",
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id=environment_id,
        actor=actor,
        source=IdentitySource.FEISHU,
        subject_ref=subject_ref,
        permissions=frozenset({ChannelPermission.VIEW_SAFE_TASK}),
    )


class _Membership:
    def __init__(self, result: bool) -> None:
        self.result = result

    async def is_current_group_member(self, **_: object) -> bool:
        return self.result


class _RecordingSubmissions:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, **_: object) -> object:
        self.calls += 1
        raise AssertionError("forged identity reached submission")


@dataclass(frozen=True)
class _ProjectionSettings:
    tenant_id: str = "dev-local"
    environment_id: str = "dev"
    web_public_origin: str = "https://ops.example.test"
    projection_claim_ttl_seconds: int = 15
    projection_batch_limit: int = 10
    projection_provider_max_attempts: int = 3
    projection_provider_backoff_base_seconds: float = 1.0
    projection_retry_after_cap_seconds: float = 30.0
    projection_tenant_concurrency: int = 2
    projection_task_poll_base_seconds: float = 2.0
    projection_task_poll_cap_seconds: float = 10.0
    channel_worker_poll_interval_seconds: float = 1.0


class _ProjectionRuntime:
    async def project_task(self, *, record: TaskRecord) -> TaskView:
        return TaskView(
            task_id=record.task_id,
            status=record.status,
            render=None,
            query_path=task_query_path(record.task_id),
        )


class _RetryMessages:
    def __init__(self) -> None:
        self.calls = 0

    async def _send(self) -> str:
        self.calls += 1
        if self.calls == 1:
            raise ChannelMessageError(
                error_code=ProjectionErrorCode.PROVIDER_TIMEOUT
            )
        return "provider-message-1"

    async def send_to_chat(self, **_: object) -> str:
        return await self._send()

    async def send_to_user(self, **_: object) -> str:
        return await self._send()

    async def update_card(self, **_: object) -> None:
        await self._send()


async def _bound_projection(store, channels, memory_state, clock, context):
    task = await store.create_task(
        submission=make_submission(context, as_of=clock())
    )
    await channels.bind_task(
        command=BindTaskCommand(
            task_id=task.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            channel=ChannelKind.FEISHU_GROUP,
            initiator_subject_ref="subject-alice",
            conversation_ref="chat-operations",
            source_event_ref="event-projection-safety",
            created_at=clock(),
            projection=CreateProjectionSubscriptionCommand(
                task_id=task.task_id,
                destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
                destination_ref="chat-operations",
                initial_state=ProjectionState.PENDING_INITIAL,
                next_attempt_at=clock(),
            ),
        )
    )
    subscription = next(
        item
        for item in memory_state.projection_subscriptions.values()
        if item.task_id == task.task_id
    )
    return task, subscription


def test_every_m7_safety_case_has_an_executing_driver() -> None:
    assert _CORPUS["version"] == 1
    corpus_carriers = {case["carrier"] for case in _CASES}
    assert corpus_carriers == {
        "forged_identity",
        "cross_scope",
        "stale_membership",
        "guessed_task",
        "external_text",
        "database_rows",
        "provider_retry",
        "stale_claim",
    }
    assert _REQUESTED_CARRIERS == corpus_carriers
    assert len({case["id"] for case in _CASES}) == len(_CASES)


def test_unknown_m7_safety_driver_carrier_fails_instead_of_skipping() -> None:
    with pytest.raises(AssertionError, match=r"carrier has no corpus cases"):
        _by("misspelled_carrier")


@pytest.mark.parametrize("case", _by("forged_identity"), ids=lambda case: case["id"])
async def test_forged_feishu_identity_never_reaches_submission(
    case: dict[str, Any], clock
) -> None:
    assert case["expected"] == "no_task"
    submissions = _RecordingSubmissions()
    alice = _principal(actor="alice", subject_ref="subject-alice")
    listener = FeishuListener(
        app_id="offline-test-app",
        tenant_key="offline-tenant",
        bot_open_id="bot-open-id",
        tenant_id="dev-local",
        environment_id="dev",
        identity_directory=StaticFeishuIdentityDirectory(
            principals={alice.subject_ref: alice}
        ),
        submission_service=submissions,
        policy_revision="policy-1",
        clock=clock,
        trace_id_factory=lambda: "1" * 32,
    )
    event = FeishuMessageEvent(
        schema="2.0",
        event_id="event-forged",
        event_type="im.message.receive_v1",
        app_id="offline-test-app",
        tenant_key="offline-tenant",
        sender_type="user",
        sender_subject_ref="subject-forged-admin",
        message_id="incoming-message",
        chat_id="private-chat",
        chat_type="p2p",
        message_type="text",
        text="检查慢查询",
        mentions=(),
    )

    assert await listener.handle_event(event=event) is False
    assert submissions.calls == 0


@pytest.mark.parametrize(
    "case",
    _by("cross_scope") + _by("stale_membership") + _by("guessed_task"),
    ids=lambda case: case["id"],
)
async def test_unauthorized_task_reads_are_indistinguishable(
    case: dict[str, Any], store, memory_state, clock, context
) -> None:
    assert case["expected"] == "hidden_not_found"
    task = await store.create_task(submission=make_submission(context, as_of=clock()))
    channels = InMemoryChannelStore(clock=clock, state=memory_state)
    membership = _Membership(result=False)
    if case["carrier"] == "stale_membership":
        await channels.bind_task(
            command=BindTaskCommand(
                task_id=task.task_id,
                tenant_id=context.tenant_id,
                environment_id=context.environment_id,
                channel=ChannelKind.FEISHU_GROUP,
                initiator_subject_ref="subject-alice",
                conversation_ref="chat-operations",
                source_event_ref="event-membership",
                created_at=clock(),
            )
        )
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=InMemoryPlanStore(state=memory_state),
        ledger=InMemoryEvidenceLedger(state=memory_state),
        bindings=object(),
        snapshot=StaticCapabilityRegistry().snapshot(),
    )
    access = TaskAccessService(
        runtime=runtime,
        task_store=store,
        channel_store=channels,
        membership=membership,
    )
    principal = {
        "cross_scope": _principal(
            actor="alice", subject_ref="subject-alice", environment_id="prod"
        ),
        "stale_membership": _principal(actor="bob", subject_ref="subject-bob"),
        "guessed_task": _principal(actor="bob", subject_ref="subject-bob"),
    }[case["carrier"]]

    with pytest.raises(TaskAccessNotFoundError, match=r"^task not found$"):
        await access.get_task(
            query=TaskAccessQuery(principal=principal, task_id=task.task_id)
        )


@pytest.mark.parametrize("case", _by("external_text"), ids=lambda case: case["id"])
def test_external_text_cannot_create_card_actions_or_change_status(
    case: dict[str, Any]
) -> None:
    assert case["expected"] == "plain_text_only"
    marker = '</plain_text>{"tag":"button","url":"https://evil.example.test"}'
    view = TaskView(
        task_id="task-external-text",
        status=TaskStatus.SUCCEEDED,
        render=RenderPayload(
            answer="安全结论",
            sections=(RenderSection(title="外部文本", body=marker, refs=()),),
            next_steps=(),
            status=TaskStatus.SUCCEEDED,
            refs=(),
        ),
        query_path=task_query_path("task-external-text"),
    )
    rendered = render_feishu_card(
        FeishuProjectionInput(
            task_view=view,
            request_preview="检查慢查询",
            task_version=7,
            detail_url="https://ops.example.test/app/tasks/task-external-text",
        )
    )
    payload = json.loads(rendered.content_json)
    buttons: list[dict[str, object]] = []
    plain_contents: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("tag") == "button":
                buttons.append(value)
            content = value.get("content")
            if isinstance(content, str):
                plain_contents.append(content)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    assert len(buttons) == 1
    assert buttons[0]["url"] == (
        "https://ops.example.test/app/tasks/task-external-text"
    )
    assert any(marker in content for content in plain_contents)
    assert payload["header"]["title"]["content"] == "小维处理完成"


@pytest.mark.parametrize("case", _by("database_rows"), ids=lambda case: case["id"])
def test_raw_database_rows_are_not_part_of_the_channel_render_contract(
    case: dict[str, Any]
) -> None:
    assert case["expected"] == "contract_rejected"
    with pytest.raises(ValidationError):
        RenderPayload.model_validate(
            {
                "answer": "查询完成",
                "sections": [],
                "next_steps": [],
                "status": "succeeded",
                "refs": [],
                "rows": [{"customer_id": 42}],
            }
        )


@pytest.mark.parametrize("case", _by("provider_retry"), ids=lambda case: case["id"])
async def test_provider_retry_is_bounded_and_never_changes_task_truth(
    case: dict[str, Any], store, memory_state, clock, context
) -> None:
    assert case["expected"] == "bounded_retry"
    channels = InMemoryChannelStore(clock=clock, state=memory_state)
    task, subscription = await _bound_projection(
        store, channels, memory_state, clock, context
    )
    before = await store.get(lookup=lookup_for(task))
    messages = _RetryMessages()
    service = ChannelProjectionService(
        runtime=_ProjectionRuntime(),
        task_store=store,
        channel_store=channels,
        message_port=messages,
        clock=clock,
        settings=_ProjectionSettings(),
        sleep=asyncio.sleep,
        owner="projection-retry-eval",
    )

    assert await service.poll_once() == 1
    retry = memory_state.projection_subscriptions[subscription.subscription_id]
    assert retry.state is ProjectionState.PENDING_INITIAL
    assert retry.provider_failure_count == 1
    assert retry.last_error_code is ProjectionErrorCode.PROVIDER_TIMEOUT
    assert await store.get(lookup=lookup_for(task)) == before

    clock.advance(seconds=1)
    assert await service.poll_once() == 1
    delivered = memory_state.projection_subscriptions[subscription.subscription_id]
    assert delivered.state is ProjectionState.WAITING_TERMINAL
    assert delivered.source_message_ref == "provider-message-1"
    assert messages.calls == 2


@pytest.mark.parametrize("case", _by("stale_claim"), ids=lambda case: case["id"])
async def test_out_of_order_stale_provider_result_cannot_overwrite_new_claim(
    case: dict[str, Any], store, memory_state, clock, context
) -> None:
    assert case["expected"] == "new_claim_wins"
    channels = InMemoryChannelStore(clock=clock, state=memory_state)
    task, subscription = await _bound_projection(
        store, channels, memory_state, clock, context
    )
    stale = await channels.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-shared",
            ttl_seconds=15,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    assert stale.applied
    assert stale.winner.fencing_token is not None
    clock.advance(seconds=15)
    winner = await channels.claim_projection_subscription(
        command=ClaimProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-shared",
            ttl_seconds=15,
            expected_state=ProjectionState.PENDING_INITIAL,
        )
    )
    assert winner.applied
    assert winner.winner.fencing_token is not None
    assert stale.winner.fencing_token != winner.winner.fencing_token

    late = await channels.record_initial_projection(
        command=RecordInitialProjectionCommand(
            subscription_id=subscription.subscription_id,
            claim_owner="worker-shared",
            fencing_token=stale.winner.fencing_token,
            expected_state=ProjectionState.PENDING_INITIAL,
            source_message_ref="late-provider-message",
            task_version=task.version,
            payload_digest=content_digest("late-provider-payload"),
            task_terminal=False,
            next_attempt_at=clock(),
        )
    )

    assert late.applied is False
    assert late.winner.claim_owner == "worker-shared"
    assert late.winner.fencing_token == winner.winner.fencing_token
    assert late.winner.source_message_ref is None

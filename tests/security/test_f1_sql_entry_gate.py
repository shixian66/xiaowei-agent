"""SQL 形状的文本在每个文本入口都不进入模型，也不成为普通对话提交（设计 §4 第 3 条、§5.1）。

识别只判断“是不是 SQL”，不判断是否支持或安全：完整有效的 SQL、已知 StarRocks 语句族（含清单外
语句、多语句、hint、前置注释）与明确标记为 sql 的代码块都保存为 SqlArtifact，再由 SQLGuard 给出
确定性拒绝；以 SQL 关键字开头的自然语言继续走对话。飞书与 Web 经渠道提交服务
进入 SqlArtifact；API/CLI 本阶段不支持 SQL 提交，在创建任务前确定性拒绝。
"""

import asyncio
import datetime as dt
import io
import json
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import httpx
import pytest
from tests.fakes.model import ScriptedModelAdapter
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.channel_submission import ChannelSubmissionService
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AdvisoryModelResult,
    ArtifactSubmission,
    AuthenticatedPrincipal,
    ChannelPermission,
    ConversationSubmission,
    IdentitySource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelAdvisory,
    ModelUsage,
    ReadinessReport,
    TaskLookup,
    TaskStatus,
)
from xiaowei_agent.interfaces.api import create_app
from xiaowei_agent.interfaces.cli import run_cli
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.feishu_listener import FeishuListener
from xiaowei_agent.interfaces.feishu_sdk import FeishuMessageEvent
from xiaowei_agent.persistence.fake import InMemoryChannelStore

pytestmark = pytest.mark.security

_MARKER = "secret_marker_x"
_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)

SQL_SHAPED: tuple[str, ...] = (
    # sqlglot 降级为 Command、清单外（“暂未支持”）
    f"SHOW USERS -- {_MARKER}",
    # 多语句
    f"SHOW BACKENDS; SHOW FRONTENDS -- {_MARKER}",
    # 已知 StarRocks 语句族、sqlglot 解析不了
    f"ADMIN SHOW FRONTEND CONFIG -- {_MARKER}",
    # 明确标记为 sql 的代码块
    f"```sql\nshow me {_MARKER}\n```",
    # StarRocks 3.2+ 预处理语句与 MySQL 兼容写语句（登记表之前漏登记，会进模型）
    f"PREPARE p FROM 'SHOW {_MARKER}'",
    f"EXECUTE {_MARKER}",
    f"DEALLOCATE PREPARE {_MARKER}",
    f"DROP PREPARE {_MARKER}",
    f"RENAME TABLE {_MARKER} TO new_name",
    # hint
    f"SHOW /*+ SET_VAR({_MARKER}=1) */ BACKENDS",
    # 前置注释 + 多语句
    f"-- {_MARKER}\nSELECT 1; DROP TABLE t",
    # 代码块里前置 hint / 注释
    f"```sql\n/*+ SET_VAR({_MARKER}=1) */ SELECT 1\n```",
    f"```sql\n-- {_MARKER}\nSHOW BACKENDS; SHOW FRONTENDS\n```",
)

CONVERSATION = "show 一下最近30分钟的慢查询"

# 以 SQL 关键字开头的自然语言：不是已知 StarRocks 语句族、也不是完整有效的 SQL，继续走对话。
NATURAL_LANGUAGE: tuple[str, ...] = (
    CONVERSATION,
    "show me the slow queries",
    "create a dashboard",
    "analyze this",
    "show status of my task",
    "grant me access to the dashboard",
)

# 命中语句签名却不是完整语句：任务照常创建，worker 在构造模型请求前以固定文案拒绝（提示放入
# sql 代码块重发），不建 SqlArtifact，不进模型。
SQL_LIKE: tuple[str, ...] = (
    "show data for yesterday",
    "create table for this report",
    "prepare a report",
    "execute the plan",
    "SELECT 'unterminated",
)


def _model() -> ScriptedModelAdapter:
    return ScriptedModelAdapter(
        interaction=InteractionModelResult(
            draft=InteractionDraft(
                proposed_kind=InteractionKind.CONVERSATION,
                confidence=0.9,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(),
        ),
        advisory=AdvisoryModelResult(
            advisory=ModelAdvisory(analysis="不应被调用", suggestions=(), uncertainties=()),
            usage=ModelUsage(),
        ),
    )


def _lookup(harness: RuntimeHarness, task_id: str) -> TaskLookup:
    return TaskLookup(
        task_id=task_id,
        tenant_id=harness.context.tenant_id,
        environment_id=harness.context.environment_id,
    )


async def _execute(harness: RuntimeHarness, task_id: str) -> Any:
    attempt = await harness.store.begin_task_attempt(
        command=harness.attempt_command(task_id)
    )
    submission = await harness.store.get_submission(lookup=_lookup(harness, task_id))
    return await harness.runtime.execute_task(grant=attempt.grant, submission=submission)


def _non_sql_state(harness: RuntimeHarness) -> str:
    """除 SqlArtifactStore 外的全部持久事实、trace 与审计。"""
    state = harness.state
    parts: list[object] = [
        [task.model_dump(mode="json") for task in state.tasks.values()],
        [submission.model_dump(mode="json") for submission in state.submissions.values()],
        [artifact.model_dump(mode="json") for artifact in state.interaction_artifacts.values()],
        [binding.model_dump(mode="json") for binding in state.channel_bindings.values()],
        [
            subscription.model_dump(mode="json")
            for subscription in state.projection_subscriptions.values()
        ],
        [event.model_dump(mode="json") for event in harness.sink.events],
        [
            event.model_dump(mode="json")
            for events in state.audit_events.values()
            for event in events
        ],
    ]
    return json.dumps(parts, ensure_ascii=False, default=str)


# --- 飞书：正式 listener → 渠道提交服务 → worker ------------------------------


def _feishu(harness: RuntimeHarness) -> FeishuListener:
    principal = AuthenticatedPrincipal(
        tenant_id=harness.context.tenant_id,
        environment_id=harness.context.environment_id,
        actor=harness.context.actor,
        source=IdentitySource.FEISHU,
        subject_ref="user-open-id",
        permissions=frozenset(
            {ChannelPermission.VIEW_SAFE_TASK, ChannelPermission.SUBMIT_READONLY_TASK}
        ),
    )
    service = ChannelSubmissionService(
        runtime=harness.runtime._task_views,
        channel_store=InMemoryChannelStore(clock=harness.clock, state=harness.state),
    )
    return FeishuListener(
        app_id="cli_test_app",
        tenant_key="tenant-test",
        bot_open_id="bot-open-id",
        tenant_id=harness.context.tenant_id,
        environment_id=harness.context.environment_id,
        identity_directory=StaticFeishuIdentityDirectory(
            principals={principal.subject_ref: principal}
        ),
        submission_service=service,
        activation_service=object(),  # type: ignore[arg-type]
        activation_notifications=object(),  # type: ignore[arg-type]
        policy_revision=harness.context.policy_revision,
        clock=lambda: _NOW,
        trace_id_factory=lambda: "1" * 32,
    )


def _event(text: str) -> FeishuMessageEvent:
    return FeishuMessageEvent(
        schema="2.0",
        event_id="event-1",
        event_type="im.message.receive_v1",
        app_id="cli_test_app",
        tenant_key="tenant-test",
        sender_type="user",
        sender_subject_ref="user-open-id",
        message_id="message-1",
        chat_id="chat-1",
        chat_type="p2p",
        message_type="text",
        text=text,
        mentions=(),
    )


@pytest.mark.parametrize("text", SQL_SHAPED)
async def test_feishu_sql_shaped_text_becomes_an_artifact_and_never_reaches_the_model(
    text: str,
) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)

    assert await _feishu(harness).handle_event(event=_event(text)) is True

    (task_id,) = harness.state.tasks
    submission = await harness.store.get_submission(lookup=_lookup(harness, task_id))
    assert isinstance(submission, ArtifactSubmission)
    (artifact,) = harness.state.sql_artifacts.values()
    assert _MARKER in artifact.sql_bytes.decode()

    outcome = await _execute(harness, task_id)

    assert outcome.status is TaskStatus.REJECTED
    assert model.interaction_requests == []
    assert harness.calls == []
    assert _MARKER not in _non_sql_state(harness)


@pytest.mark.parametrize("text", NATURAL_LANGUAGE)
async def test_feishu_conversation_control_still_reaches_the_model(text: str) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)

    assert await _feishu(harness).handle_event(event=_event(text)) is True

    (task_id,) = harness.state.tasks
    submission = await harness.store.get_submission(lookup=_lookup(harness, task_id))
    assert isinstance(submission, ConversationSubmission)
    await _execute(harness, task_id)
    assert len(model.interaction_requests) == 1
    assert harness.state.sql_artifacts == {}


# --- API 与 CLI：本阶段不支持 SQL 提交，创建任务前确定性拒绝 --------------------


class _Ready:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(database_ok=True, revision_matches_head=True, assembled=True)


def _api(harness: RuntimeHarness) -> Any:
    return create_app(
        runtime=harness.runtime,
        settings=Settings(environment_id="dev", actor="local-developer"),
        readiness=_Ready(),
        clock=lambda: _NOW,
        policy_revision=harness.context.policy_revision,
    )


async def _post(app: Any, text: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        return await client.post(
            "/v1/tasks", json={"text": text, "idempotency_key": "api-key-1"}
        )


@pytest.mark.parametrize("text", SQL_SHAPED)
async def test_api_rejects_sql_shaped_text_before_any_task_exists(text: str) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)

    response = await _post(_api(harness), text)

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "sql_message.not_accepted"}}
    assert _MARKER not in response.text
    assert harness.state.tasks == {}
    assert harness.state.submissions == {}
    assert harness.state.sql_artifacts == {}
    assert model.interaction_requests == []
    assert _MARKER not in _non_sql_state(harness)


@pytest.mark.parametrize("text", NATURAL_LANGUAGE)
async def test_api_conversation_control_is_accepted_and_reaches_the_model(text: str) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)

    response = await _post(_api(harness), text)

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    await _execute(harness, task_id)
    assert len(model.interaction_requests) == 1


class _AsgiOpener:
    """把 CLI 的 urllib 请求原样交给 API ASGI 应用；只在同步测试中使用。"""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.statuses: list[int] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app, raise_app_exceptions=False),
                base_url="http://testserver",
            ) as client:
                return await client.request(
                    request.get_method(),
                    request.full_url.removeprefix("http://127.0.0.1:8000"),
                    content=request.data,  # type: ignore[arg-type]
                    headers=dict(request.header_items()),
                )

        response = asyncio.run(send())
        self.statuses.append(response.status_code)
        if response.status_code >= 400:
            headers = Message()
            headers["content-type"] = response.headers["content-type"]
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                "error",
                headers,
                io.BytesIO(response.content),
            )
        raise AssertionError("CLI SQL submission must not be accepted")


@pytest.mark.parametrize("text", SQL_SHAPED[:2])
def test_cli_sql_shaped_text_is_rejected_by_the_api_before_any_task(text: str) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)
    opener = _AsgiOpener(_api(harness))
    stdout, stderr = io.StringIO(), io.StringIO()

    code = run_cli(
        ["task", "submit", "--text", text, "--idempotency-key", "cli-key-1"],
        opener=opener,
        stdout=stdout,
        stderr=stderr,
    )

    assert opener.statuses == [422]
    assert code == 9
    assert stderr.getvalue() == "xiaowei: sql_message_not_accepted\n"
    assert _MARKER not in stdout.getvalue() + stderr.getvalue()
    assert harness.state.tasks == {}
    assert model.interaction_requests == []


# --- 升级前已持久化的对话任务：worker 在构造模型请求前兼容保护 ------------------


async def _legacy_conversation(harness: RuntimeHarness, text: str, *, bound: bool) -> str:
    """直接写入升级前的 ConversationSubmission，绕过今天的入口识别。"""
    from tests.conftest import make_envelope, make_submission

    from xiaowei_agent.contracts import Channel, ChannelKind
    from xiaowei_agent.persistence.channel import BindTaskCommand

    record = await harness.store.create_task(
        submission=make_submission(
            harness.context,
            envelope=make_envelope(
                text=text,
                channel=Channel.FEISHU,
                tenant_id=harness.context.tenant_id,
                environment_id=harness.context.environment_id,
                actor=harness.context.actor,
                idempotency_key=f"channel:v1:{'c' * 64}",
            ),
        )
    )
    if bound:
        channels = InMemoryChannelStore(clock=harness.clock, state=harness.state)
        await channels.bind_task(
            command=BindTaskCommand(
                task_id=record.task_id,
                tenant_id=harness.context.tenant_id,
                environment_id=harness.context.environment_id,
                channel=ChannelKind.FEISHU_PRIVATE,
                initiator_subject_ref="user-open-id",
                source_event_ref="c" * 64,
                created_at=_NOW,
            )
        )
    return record.task_id


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
@pytest.mark.parametrize("text", [f"SHOW USERS -- {_MARKER}", *SQL_SHAPED[1:]])
async def test_legacy_sql_shaped_conversation_is_rejected_before_the_model(
    text: str, bound: bool
) -> None:
    from xiaowei_agent.contracts import InteractionRejectionReasonCode
    from xiaowei_agent.rendering.generic import EMBEDDED_SQL_REJECTED

    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)
    task_id = await _legacy_conversation(harness, text, bound=bound)

    outcome = await _execute(harness, task_id)

    assert outcome.status is TaskStatus.REJECTED
    record = await harness.store.get(lookup=_lookup(harness, task_id))
    assert record.terminal_reason == (
        InteractionRejectionReasonCode.EMBEDDED_SQL_NOT_EXECUTED.value
    )
    view = await harness.runtime.query_task(lookup=_lookup(harness, task_id))
    assert view.render is not None and view.render.answer == EMBEDDED_SQL_REJECTED
    assert model.interaction_requests == []
    assert harness.calls == []
    assert harness.state.sql_artifacts == {}


@pytest.mark.parametrize("text", NATURAL_LANGUAGE)
async def test_legacy_natural_language_conversation_still_reaches_the_model(
    text: str,
) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)
    task_id = await _legacy_conversation(harness, text, bound=True)

    await _execute(harness, task_id)

    assert len(model.interaction_requests) == 1


# --- 像 SQL 却不是完整语句：各入口都只给固定提示，不进模型 ------------------------


def _assert_sql_like_rejected(harness: RuntimeHarness, model: ScriptedModelAdapter) -> None:
    from xiaowei_agent.contracts import InteractionRejectionReasonCode

    (record,) = harness.state.tasks.values()
    assert record.status is TaskStatus.REJECTED
    assert record.terminal_reason == (
        InteractionRejectionReasonCode.SQL_LIKE_TEXT_NOT_EXECUTED.value
    )
    assert model.interaction_requests == []
    assert harness.calls == []
    assert harness.state.sql_artifacts == {}


@pytest.mark.parametrize("text", SQL_LIKE)
async def test_feishu_sql_like_text_gets_a_fixed_prompt_without_the_model(text: str) -> None:
    from xiaowei_agent.rendering.generic import SQL_LIKE_TEXT_REJECTED

    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)

    assert await _feishu(harness).handle_event(event=_event(text)) is True

    (task_id,) = harness.state.tasks
    submission = await harness.store.get_submission(lookup=_lookup(harness, task_id))
    assert isinstance(submission, ConversationSubmission)
    await _execute(harness, task_id)
    _assert_sql_like_rejected(harness, model)
    view = await harness.runtime.query_task(lookup=_lookup(harness, task_id))
    assert view.render is not None and view.render.answer == SQL_LIKE_TEXT_REJECTED


@pytest.mark.parametrize("text", SQL_LIKE)
async def test_api_sql_like_text_gets_a_fixed_prompt_without_the_model(text: str) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)

    response = await _post(_api(harness), text)

    assert response.status_code == 202
    await _execute(harness, response.json()["task_id"])
    _assert_sql_like_rejected(harness, model)


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
@pytest.mark.parametrize("text", SQL_LIKE)
async def test_legacy_sql_like_conversation_is_rejected_before_the_model(
    text: str, bound: bool
) -> None:
    model = _model()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=model)
    task_id = await _legacy_conversation(harness, text, bound=bound)

    await _execute(harness, task_id)

    _assert_sql_like_rejected(harness, model)

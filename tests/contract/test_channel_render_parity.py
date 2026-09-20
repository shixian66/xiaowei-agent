"""同一 TaskView 在 API、CLI、Web 与飞书上的安全语义一致性。"""

import datetime as dt
import io
import json
from email.message import Message

import httpx

from xiaowei_agent.application.channel_access import AccessibleTask
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    ClarificationField,
    ClarificationPayload,
    ClarificationReasonCode,
    EffectClass,
    ExecutionDisclosure,
    ExecutionDisclosureDisposition,
    ExecutionDisclosureStep,
    FeishuProjectionInput,
    ReadClass,
    ReadinessReport,
    RenderPayload,
    RenderSection,
    TaskStatus,
    TaskView,
    task_query_path,
)
from xiaowei_agent.interfaces.api import create_app as create_internal_app
from xiaowei_agent.interfaces.cli import run_cli
from xiaowei_agent.interfaces.web_models import WebTaskDetail
from xiaowei_agent.rendering.feishu import render_feishu_card
from xiaowei_agent.rendering.generic import render_conversation_response

_NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)


def _disclosure() -> ExecutionDisclosure:
    return ExecutionDisclosure(
        capability_id="starrocks.slow_query.diagnose",
        capability_version="1.0.0",
        environment_id="dev",
        provider="starrocks",
        resource_kind="cluster",
        resource_ids=("starrocks-dev-1",),
        pure_read_only=True,
        plan_disposition=ExecutionDisclosureDisposition.BOUNDED_READ,
        read_classes=(ReadClass.BOUNDED,),
        has_side_effect=False,
        steps=(
            ExecutionDisclosureStep(
                step_id="s1",
                operation="list_slow_queries",
                effect_class=EffectClass.READ,
                read_class=ReadClass.BOUNDED,
                side_effect=False,
            ),
        ),
        external_target_access=True,
    )


def _view(
    *,
    answer: str = "查询完成，未发现越界结果。",
    advisory_body: str = "扫描行数偏高，建议检查分区裁剪。",
) -> TaskView:
    task_id = "task-channel-parity"
    return TaskView(
        task_id=task_id,
        status=TaskStatus.SUCCEEDED,
        render=RenderPayload(
            answer=answer,
            sections=(
                RenderSection(
                    title="证据摘要",
                    body="过去三十分钟慢查询数量为 2。",
                    refs=("evidence:query-count",),
                ),
                RenderSection(
                    title="安全边界",
                    body="这里只展示安全摘要，不展示数据库真实结果行。",
                    refs=("policy:database-result-boundary",),
                ),
                RenderSection(
                    title="限制",
                    body="结论仅覆盖当前时间窗与只读证据。",
                    refs=("evidence:query-count",),
                ),
                RenderSection(
                    title="模型分析（仅供参考）",
                    body=advisory_body,
                    refs=(),
                ),
            ),
            next_steps=("继续观察指标变化。",),
            status=TaskStatus.SUCCEEDED,
            refs=("trace:channel-parity", "evidence:root"),
        ),
        disclosure=_disclosure(),
        query_path=task_query_path(task_id),
    )


def _clarification_view() -> TaskView:
    task_id = "task-channel-clarification"
    return TaskView(
        task_id=task_id,
        status=TaskStatus.CLARIFICATION_REQUIRED,
        clarification=ClarificationPayload(
            reason_code=ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS,
            missing_fields=(ClarificationField.TIME_RANGE,),
            prompt="请补充你要处理的具体目标。",
        ),
        query_path=task_query_path(task_id),
    )


class _Runtime:
    def __init__(self, view: TaskView) -> None:
        self.view = view

    async def submit_task(self, *, submission: object) -> TaskView:
        raise AssertionError(submission)

    async def query_task(self, *, lookup: object) -> TaskView:
        return self.view


class _Probe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            database_ok=True,
            revision_matches_head=True,
            assembled=True,
        )


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200
        self.headers = Message()
        self.headers["content-type"] = "application/json"

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def close(self) -> None:
        return None


def _plain_card_text(value: object) -> str:
    values: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "content" and isinstance(child, str):
                    values.append(child)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return "\n".join(values)


async def test_all_thin_channels_preserve_one_terminal_render_payload() -> None:
    view = _view()
    settings = Settings(environment_id="dev")
    app = create_internal_app(
        runtime=_Runtime(view),
        settings=settings,
        readiness=_Probe(),
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        api_response = await client.get(view.query_path)

    stdout = io.StringIO()
    stderr = io.StringIO()
    cli_code = run_cli(
        ["task", "get", view.task_id],
        opener=lambda *_args, **_kwargs: _Response(api_response.content),
        stdout=stdout,
        stderr=stderr,
    )
    web = WebTaskDetail.from_accessible(
        AccessibleTask(
            task_view=view,
            request_preview="检查最近三十分钟慢查询",
            submitted_at=_NOW,
            task_version=7,
        )
    )
    feishu = render_feishu_card(
        FeishuProjectionInput(
            task_view=view,
            request_preview="检查最近三十分钟慢查询",
            task_version=7,
            detail_url="https://ops.example.test/app/tasks/task-channel-parity",
        )
    )

    canonical = view.model_dump(mode="json")
    assert api_response.status_code == 200
    assert api_response.json() == canonical
    assert cli_code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == canonical
    assert web.status is view.status
    assert web.render == view.render
    assert web.disclosure == view.disclosure
    assert web.task_id == view.task_id

    assert feishu.truncated is False
    card = json.loads(feishu.content_json)
    card_text = _plain_card_text(card)
    assert "任务状态：已完成" in card_text
    assert "执行披露" in card_text
    assert "bounded_read" in card_text
    assert "已送达" not in card_text
    assert "已确认" not in card_text
    assert view.task_id in card_text
    assert view.render is not None
    assert len(view.render.sections) == 4
    assert view.render.sections[3].title == "模型分析（仅供参考）"
    for value in (
        view.render.answer,
        *(section.title for section in view.render.sections),
        *(section.body for section in view.render.sections),
        *(ref for section in view.render.sections for ref in section.refs),
        *view.render.next_steps,
        *view.render.refs,
    ):
        assert value in card_text


def test_feishu_clips_the_fourth_advisory_but_web_keeps_the_full_payload() -> None:
    advisory_body = "模型建议" * 200
    view = _view(advisory_body=advisory_body)
    rendered = render_feishu_card(
        FeishuProjectionInput(
            task_view=view,
            request_preview="检查最近三十分钟慢查询",
            task_version=7,
            detail_url="https://ops.example.test/app/tasks/task-channel-parity",
        )
    )

    assert rendered.truncated is True
    card = json.loads(rendered.content_json)
    text = _plain_card_text(card)
    assert "模型分析（仅供参考）" in text
    assert advisory_body not in text
    assert "模型建议" in text
    assert "还有更多证据，请打开详情查看完整结果。" in text
    action = card["elements"][-1]["actions"][0]
    assert action["url"] == (
        "https://ops.example.test/app/tasks/task-channel-parity"
    )
    web = WebTaskDetail.from_accessible(
        AccessibleTask(
            task_view=view,
            request_preview="检查最近三十分钟慢查询",
            submitted_at=_NOW,
            task_version=7,
        )
    )
    assert web.render == view.render
    assert web.render is not None
    assert web.render.sections[3].body == advisory_body


async def test_channels_preserve_clarification_projection() -> None:
    view = _clarification_view()
    settings = Settings(environment_id="dev")
    app = create_internal_app(
        runtime=_Runtime(view),
        settings=settings,
        readiness=_Probe(),
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        api_response = await client.get(view.query_path)

    stdout = io.StringIO()
    stderr = io.StringIO()
    cli_code = run_cli(
        ["task", "get", view.task_id],
        opener=lambda *_args, **_kwargs: _Response(api_response.content),
        stdout=stdout,
        stderr=stderr,
    )
    web = WebTaskDetail.from_accessible(
        AccessibleTask(
            task_view=view,
            request_preview="你自己看着办",
            submitted_at=_NOW,
            task_version=3,
        )
    )
    feishu = render_feishu_card(
        FeishuProjectionInput(
            task_view=view,
            request_preview="你自己看着办",
            task_version=3,
            detail_url="https://ops.example.test/app/tasks/task-channel-clarification",
        )
    )

    canonical = view.model_dump(mode="json")
    assert api_response.status_code == 200
    assert api_response.json() == canonical
    assert cli_code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == canonical
    assert web.status is TaskStatus.CLARIFICATION_REQUIRED
    assert web.render is None
    assert web.clarification == view.clarification

    card_text = _plain_card_text(json.loads(feishu.content_json))
    assert "任务状态：需要补充信息" in card_text
    assert view.clarification is not None
    assert view.clarification.prompt in card_text


async def test_conversation_capability_catalog_survives_every_thin_channel() -> None:
    """I2-B 的回答比之前长一截：一段结论 + 每条能力一节 + 来源引用。

    之前那句固定文案短到任何渠道都装得下，所以"对话回答能不能完整送达"从来没被
    验证过。能力目录会随 Registry 增长，飞书卡片有大小上限——`truncated` 一旦为真，
    用户看到的就是一份**被截断的能力清单**，那比不回答更糟：他会以为小维只有前几项
    能力。这条把四个渠道和截断标志一起钉住。
    """
    snapshot = StaticCapabilityRegistry().snapshot()
    payload = render_conversation_response(snapshot=snapshot)
    task_id = "task-channel-conversation"
    view = TaskView(
        task_id=task_id,
        status=TaskStatus.SUCCEEDED,
        render=payload,
        clarification=None,
        disclosure=None,
        query_path=task_query_path(task_id),
    )
    settings = Settings(environment_id="dev")
    app = create_internal_app(
        runtime=_Runtime(view),
        settings=settings,
        readiness=_Probe(),
        clock=lambda: _NOW,
        policy_revision="policy-2026-09-01",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        api_response = await client.get(view.query_path)

    stdout = io.StringIO()
    stderr = io.StringIO()
    cli_code = run_cli(
        ["task", "get", task_id],
        opener=lambda *_args, **_kwargs: _Response(api_response.content),
        stdout=stdout,
        stderr=stderr,
    )
    web = WebTaskDetail.from_accessible(
        AccessibleTask(
            task_view=view,
            request_preview="你能做什么？",
            submitted_at=_NOW,
            task_version=1,
        )
    )
    feishu = render_feishu_card(
        FeishuProjectionInput(
            task_view=view,
            request_preview="你能做什么？",
            task_version=1,
            detail_url=f"https://ops.example.test/app/tasks/{task_id}",
        )
    )

    canonical = view.model_dump(mode="json")
    assert api_response.status_code == 200
    assert api_response.json() == canonical
    assert cli_code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == canonical
    assert web.status is TaskStatus.SUCCEEDED
    assert web.render == payload
    # 没有计划就没有执行披露；卡片不得凭空长出一个披露段落。
    assert web.disclosure is None

    assert feishu.truncated is False
    card_text = _plain_card_text(json.loads(feishu.content_json))
    assert "任务状态：已完成" in card_text
    assert "执行披露" not in card_text
    for spec in snapshot.specs:
        assert spec.capability_id in card_text
    assert payload.answer in card_text
    for section in payload.sections:
        assert section.body in card_text

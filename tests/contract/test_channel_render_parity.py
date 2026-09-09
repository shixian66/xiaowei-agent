"""同一 TaskView 在 API、CLI、Web 与飞书上的安全语义一致性。"""

import datetime as dt
import io
import json
from email.message import Message

import httpx

from xiaowei_agent.application.channel_access import AccessibleTask
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    FeishuProjectionInput,
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

_NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)


def _view(*, answer: str = "查询完成，未发现越界结果。") -> TaskView:
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
            ),
            next_steps=("继续观察指标变化。",),
            status=TaskStatus.SUCCEEDED,
            refs=("trace:channel-parity", "evidence:root"),
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
    assert web.task_id == view.task_id

    assert feishu.truncated is False
    card = json.loads(feishu.content_json)
    card_text = _plain_card_text(card)
    assert "任务状态：已完成" in card_text
    assert view.task_id in card_text
    assert view.render is not None
    for value in (
        view.render.answer,
        *(section.title for section in view.render.sections),
        *(section.body for section in view.render.sections),
        *(ref for section in view.render.sections for ref in section.refs),
        *view.render.next_steps,
        *view.render.refs,
    ):
        assert value in card_text


def test_feishu_only_truncates_for_its_explicit_provider_budget() -> None:
    view = _view(answer="安全证据" * 400)
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
    assert "还有更多证据，请打开详情查看完整结果。" in text
    action = card["elements"][-1]["actions"][0]
    assert action["url"] == (
        "https://ops.example.test/app/tasks/task-channel-parity"
    )
    assert WebTaskDetail.from_accessible(
        AccessibleTask(
            task_view=view,
            request_preview="检查最近三十分钟慢查询",
            submitted_at=_NOW,
            task_version=7,
        )
    ).render == view.render

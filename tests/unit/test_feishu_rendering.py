"""飞书卡片只投影安全 ``TaskView``，并在供应商预算内显式截断。"""

import json

import pytest

from xiaowei_agent.contracts import (
    FeishuProjectionInput,
    RenderPayload,
    RenderSection,
    TaskStatus,
    TaskView,
    content_digest,
    task_query_path,
)
from xiaowei_agent.rendering import feishu as feishu_renderer
from xiaowei_agent.rendering.feishu import render_feishu_card


def _view(
    status: TaskStatus,
    *,
    answer: str = "查询完成，当前没有发现异常。",
    sections: tuple[RenderSection, ...] = (),
    next_steps: tuple[str, ...] = ("继续观察指标变化。",),
    refs: tuple[str, ...] = ("evidence:summary-1",),
) -> TaskView:
    task_id = "task-1"
    render = None
    if status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELED,
        TaskStatus.INDETERMINATE,
    }:
        render = RenderPayload(
            answer=answer,
            sections=sections,
            next_steps=next_steps,
            status=status,
            refs=refs,
        )
    return TaskView(
        task_id=task_id,
        status=status,
        render=render,
        query_path=task_query_path(task_id),
    )


def _projection(status: TaskStatus, **view_updates: object) -> FeishuProjectionInput:
    return FeishuProjectionInput(
        task_view=_view(status, **view_updates),
        request_preview="检查最近三十分钟的慢查询",
        task_version=3,
        detail_url="https://ops.example.test/app/tasks/task-1",
    )


def _plain_text_contents(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("tag") == "plain_text" and isinstance(value.get("content"), str):
            found.append(value["content"])
        for child in value.values():
            found.extend(_plain_text_contents(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_plain_text_contents(child))
    return found


def test_status_presentation_is_exhaustive() -> None:
    assert set(feishu_renderer._STATUS_PRESENTATION) == set(TaskStatus)


def test_nonterminal_card_is_an_acceptance_projection() -> None:
    card = render_feishu_card(_projection(TaskStatus.RUNNING))
    payload = json.loads(card.content_json)
    visible = "\n".join(_plain_text_contents(payload))

    assert payload["header"]["template"] == "blue"
    assert "小维处理中" in visible
    assert "检查最近三十分钟的慢查询" in visible
    assert "任务版本：3" in visible
    assert card.truncated is False
    assert card.payload_digest == content_digest(card.content_json)
    assert payload["config"]["enable_forward"] is False


@pytest.mark.parametrize(
    ("status", "title", "template"),
    [
        (TaskStatus.SUCCEEDED, "小维处理完成", "green"),
        (TaskStatus.FAILED, "小维处理失败", "red"),
        (TaskStatus.REJECTED, "小维已拒绝", "orange"),
        (TaskStatus.CANCELED, "小维已取消", "grey"),
        (TaskStatus.INDETERMINATE, "小维结果待确认", "yellow"),
    ],
)
def test_every_terminal_status_keeps_its_own_card_semantics(
    status: TaskStatus, title: str, template: str
) -> None:
    card = render_feishu_card(_projection(status))
    payload = json.loads(card.content_json)

    assert payload["header"] == {
        "template": template,
        "title": {"content": title, "tag": "plain_text"},
    }


def test_terminal_card_preserves_safe_render_order_and_detail_link() -> None:
    card = render_feishu_card(
        _projection(
            TaskStatus.SUCCEEDED,
            answer="结论文本",
            sections=(
                RenderSection(
                    title="证据一",
                    body="安全摘要一",
                    refs=("section-ref-1",),
                ),
                RenderSection(
                    title="证据二",
                    body="安全摘要二",
                    refs=(),
                ),
            ),
            next_steps=("下一步一", "下一步二"),
            refs=("root-ref-1", "root-ref-2"),
        )
    )
    payload = json.loads(card.content_json)
    visible = "\n".join(_plain_text_contents(payload))

    positions = [
        visible.index(text)
        for text in (
            "结论文本",
            "证据一",
            "证据二",
            "下一步一",
            "root-ref-1",
        )
    ]
    assert positions == sorted(positions)
    assert payload["elements"][-1] == {
        "actions": [
            {
                "tag": "button",
                "text": {"content": "查看完整结果", "tag": "plain_text"},
                "type": "primary",
                "url": "https://ops.example.test/app/tasks/task-1",
            }
        ],
        "tag": "action",
    }


def test_card_budget_is_bounded_and_announces_omitted_evidence() -> None:
    sections = tuple(
        RenderSection(
            title=f"证据 {index}",
            body="证据正文" * 1000,
            refs=(f"section-ref-{index}",),
        )
        for index in range(12)
    )
    card = render_feishu_card(
        FeishuProjectionInput(
            task_view=_view(
                TaskStatus.SUCCEEDED,
                answer="结论" * 3000,
                sections=sections,
                next_steps=tuple(f"建议 {index} " * 200 for index in range(10)),
                refs=tuple(f"root-ref-{index}" for index in range(20)),
            ),
            request_preview="请求" * 4000,
            task_version=99,
            detail_url="https://ops.example.test/app/tasks/task-1",
        )
    )
    payload = json.loads(card.content_json)
    visible = "\n".join(_plain_text_contents(payload))

    assert card.truncated is True
    assert "还有更多证据" in visible
    assert len(card.content_json.encode("utf-8")) <= 28_000
    assert "证据 11" not in visible
    assert "root-ref-19" not in visible


def test_json_escape_expansion_is_also_reduced_to_the_byte_budget() -> None:
    escape_heavy = "\u0001" * 8192
    card = render_feishu_card(
        _projection(
            TaskStatus.SUCCEEDED,
            answer=escape_heavy,
            sections=tuple(
                RenderSection(
                    title=escape_heavy,
                    body=escape_heavy,
                    refs=(escape_heavy, escape_heavy),
                )
                for _ in range(4)
            ),
            next_steps=(escape_heavy,) * 4,
            refs=(escape_heavy,) * 6,
        ).model_copy(update={"request_preview": escape_heavy})
    )

    assert card.truncated is True
    assert len(card.content_json.encode("utf-8")) <= 28_000
    assert "还有更多证据" in card.content_json


def test_all_dynamic_text_is_plain_text_not_executable_card_markup() -> None:
    injected = '<at id="all">所有人</at> [点我](https://evil.example) **加粗**'
    card = render_feishu_card(
        _projection(
            TaskStatus.SUCCEEDED,
            answer=injected,
            sections=(RenderSection(title=injected, body=injected, refs=()),),
            next_steps=(injected,),
            refs=(injected,),
        ).model_copy(update={"request_preview": injected})
    )
    payload = json.loads(card.content_json)

    assert '"tag":"markdown"' not in card.content_json
    assert any(injected in content for content in _plain_text_contents(payload))
    assert set(payload) == {"config", "elements", "header"}
    assert "facts" not in payload
    assert "rows" not in payload
    assert "sql" not in payload


def test_same_projection_has_a_stable_serialisation_and_digest() -> None:
    projection = _projection(TaskStatus.SUCCEEDED)

    first = render_feishu_card(projection)
    second = render_feishu_card(projection)

    assert second == first

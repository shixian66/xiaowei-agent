"""把统一安全 ``TaskView`` 投影成有界飞书卡片。"""

import json
from typing import Final, Self

from pydantic import model_validator

from xiaowei_agent.contracts import (
    Contract,
    FeishuProjectionInput,
    NonEmptyText,
    Sha256Hex,
    TaskStatus,
    content_digest,
)

_MAX_CARD_BYTES: Final[int] = 28_000
_REQUEST_CHARACTERS: Final[int] = 400
_ANSWER_CHARACTERS: Final[int] = 1_200
_SECTION_COUNT: Final[int] = 4
_SECTION_TITLE_CHARACTERS: Final[int] = 60
_SECTION_BODY_CHARACTERS: Final[int] = 500
_SECTION_REF_COUNT: Final[int] = 2
_REF_CHARACTERS: Final[int] = 120
_NEXT_STEP_COUNT: Final[int] = 4
_NEXT_STEP_CHARACTERS: Final[int] = 240
_ROOT_REF_COUNT: Final[int] = 6
_MORE_EVIDENCE_TEXT: Final[str] = "还有更多证据，请打开详情查看完整结果。"

_STATUS_PRESENTATION: Final[dict[TaskStatus, tuple[str, str, str]]] = {
    TaskStatus.CREATED: ("小维已受理", "blue", "已创建"),
    TaskStatus.PLANNING: ("小维已受理", "blue", "规划中"),
    TaskStatus.RUNNING: ("小维处理中", "blue", "执行中"),
    TaskStatus.AWAITING_APPROVAL: ("小维等待审批", "orange", "等待审批"),
    TaskStatus.SUCCEEDED: ("小维处理完成", "green", "已完成"),
    TaskStatus.FAILED: ("小维处理失败", "red", "处理失败"),
    TaskStatus.REJECTED: ("小维已拒绝", "orange", "已拒绝"),
    TaskStatus.CLARIFICATION_REQUIRED: ("小维需要补充信息", "orange", "需要补充信息"),
    TaskStatus.CANCELED: ("小维已取消", "grey", "已取消"),
    TaskStatus.INDETERMINATE: ("小维结果待确认", "yellow", "结果待确认"),
}


class RenderedFeishuCard(Contract):
    """可直接交给 typed SDK seam 的稳定 JSON 与摘要。"""

    content_json: NonEmptyText
    payload_digest: Sha256Hex
    truncated: bool

    @model_validator(mode="after")
    def _content_and_digest_are_consistent(self) -> Self:
        if len(self.content_json.encode("utf-8")) > _MAX_CARD_BYTES:
            raise ValueError("Feishu card exceeds the local byte budget")
        if self.payload_digest != content_digest(self.content_json):
            raise ValueError("Feishu card digest does not match its content")
        return self


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return f"{value[: limit - 1]}…", True


def _plain_text(content: str) -> dict[str, str]:
    return {"content": content, "tag": "plain_text"}


def _text_element(content: str) -> dict[str, object]:
    return {"tag": "div", "text": _plain_text(content)}


def _append_clipped_block(
    elements: list[dict[str, object]],
    *,
    label: str,
    value: str,
    limit: int,
) -> bool:
    clipped, truncated = _clip(value, limit)
    elements.append(_text_element(f"{label}\n{clipped}"))
    return truncated


def _serialize(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _shrink_to_byte_budget(
    payload: dict[str, object], elements: list[dict[str, object]]
) -> str:
    """序列化后从低优先级文本向前收缩，覆盖 JSON 转义膨胀。"""
    content_json = _serialize(payload)
    if len(content_json.encode("utf-8")) <= _MAX_CARD_BYTES:
        return content_json

    shrinkable: list[dict[str, object]] = []
    for element in elements[1:]:
        text = element.get("text")
        if not isinstance(text, dict):
            continue
        content = text.get("content")
        if isinstance(content, str) and content != _MORE_EVIDENCE_TEXT:
            shrinkable.append(text)

    for text in reversed(shrinkable):
        original = text["content"]
        if not isinstance(original, str):  # pragma: no cover - 上面已收窄
            continue
        text["content"] = "…"
        minimal = _serialize(payload)
        if len(minimal.encode("utf-8")) > _MAX_CARD_BYTES:
            continue

        low, high = 1, len(original)
        best = "…"
        while low <= high:
            middle = (low + high) // 2
            candidate, _ = _clip(original, middle)
            text["content"] = candidate
            serialized = _serialize(payload)
            if len(serialized.encode("utf-8")) <= _MAX_CARD_BYTES:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        text["content"] = best
        return _serialize(payload)

    raise ValueError("Feishu card fixed structure exceeds the local byte budget")


def render_feishu_card(projection: FeishuProjectionInput) -> RenderedFeishuCard:
    """只按供应商预算缩短展示，不重算任务状态、证据或下一步。"""
    view = projection.task_view
    title, template, status_label = _STATUS_PRESENTATION[view.status]
    elements: list[dict[str, object]] = []
    truncated = False

    elements.append(
        _text_element(
            "\n".join(
                (
                    f"任务状态：{status_label}",
                    f"任务编号：{view.task_id}",
                    f"任务版本：{projection.task_version}",
                )
            )
        )
    )
    truncated |= _append_clipped_block(
        elements,
        label="请求",
        value=projection.request_preview,
        limit=_REQUEST_CHARACTERS,
    )

    if view.render is not None:
        render = view.render
        truncated |= _append_clipped_block(
            elements,
            label="结论",
            value=render.answer,
            limit=_ANSWER_CHARACTERS,
        )
        if len(render.sections) > _SECTION_COUNT:
            truncated = True
        for section in render.sections[:_SECTION_COUNT]:
            section_title, title_truncated = _clip(
                section.title, _SECTION_TITLE_CHARACTERS
            )
            section_body, body_truncated = _clip(
                section.body, _SECTION_BODY_CHARACTERS
            )
            truncated |= title_truncated or body_truncated
            section_lines = [section_title, section_body]
            if len(section.refs) > _SECTION_REF_COUNT:
                truncated = True
            if section.refs:
                safe_refs: list[str] = []
                for ref in section.refs[:_SECTION_REF_COUNT]:
                    clipped_ref, ref_truncated = _clip(ref, _REF_CHARACTERS)
                    truncated |= ref_truncated
                    safe_refs.append(clipped_ref)
                section_lines.append("引用：" + "、".join(safe_refs))
            elements.append(_text_element("\n".join(section_lines)))

        if len(render.next_steps) > _NEXT_STEP_COUNT:
            truncated = True
        if render.next_steps:
            next_steps: list[str] = []
            for index, step in enumerate(render.next_steps[:_NEXT_STEP_COUNT], start=1):
                clipped_step, step_truncated = _clip(step, _NEXT_STEP_CHARACTERS)
                truncated |= step_truncated
                next_steps.append(f"{index}. {clipped_step}")
            elements.append(_text_element("下一步\n" + "\n".join(next_steps)))

        if len(render.refs) > _ROOT_REF_COUNT:
            truncated = True
        if render.refs:
            refs: list[str] = []
            for ref in render.refs[:_ROOT_REF_COUNT]:
                clipped_ref, ref_truncated = _clip(ref, _REF_CHARACTERS)
                truncated |= ref_truncated
                refs.append(clipped_ref)
            elements.append(_text_element("证据引用\n" + "\n".join(refs)))

    if truncated:
        elements.append(_text_element(_MORE_EVIDENCE_TEXT))
    elements.append(
        {
            "actions": [
                {
                    "tag": "button",
                    "text": _plain_text("查看完整结果"),
                    "type": "primary",
                    "url": str(projection.detail_url),
                }
            ],
            "tag": "action",
        }
    )
    payload: dict[str, object] = {
        "config": {
            "enable_forward": False,
            "update_multi": True,
            "wide_screen_mode": True,
        },
        "elements": elements,
        "header": {
            "template": template,
            "title": _plain_text(title),
        },
    }
    content_json = _serialize(payload)
    if len(content_json.encode("utf-8")) > _MAX_CARD_BYTES:
        if not truncated:
            elements.insert(-1, _text_element(_MORE_EVIDENCE_TEXT))
            truncated = True
        content_json = _shrink_to_byte_budget(payload, elements)
    return RenderedFeishuCard(
        content_json=content_json,
        payload_digest=content_digest(content_json),
        truncated=truncated,
    )


__all__ = ["RenderedFeishuCard", "render_feishu_card"]

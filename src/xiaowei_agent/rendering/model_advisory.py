"""已验证 ModelAdvisory 到统一 RenderPayload 的纯文本追加。"""

from xiaowei_agent.contracts import ModelAdvisory, RenderPayload, RenderSection

_TITLE = "模型分析（仅供参考）"


def append_model_advisory(
    payload: RenderPayload, *, advisory: ModelAdvisory | None
) -> RenderPayload:
    """只追加说明段；不修改确定性事实、状态、引用或下一步。"""
    if advisory is None:
        return payload
    lines = [advisory.analysis]
    if advisory.suggestions:
        lines.append("建议：")
        lines.extend(f"- {item}" for item in advisory.suggestions)
    if advisory.uncertainties:
        lines.append("未确认：")
        lines.extend(f"- {item}" for item in advisory.uncertainties)
    section = RenderSection(title=_TITLE, body="\n".join(lines), refs=())
    return RenderPayload(
        answer=payload.answer,
        sections=(*payload.sections, section),
        next_steps=payload.next_steps,
        status=payload.status,
        refs=payload.refs,
    )


__all__ = ["append_model_advisory"]

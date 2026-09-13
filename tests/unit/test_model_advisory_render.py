"""模型 advisory 只能增加第四个纯文本说明段。"""

from xiaowei_agent.contracts import (
    ModelAdvisory,
    RenderPayload,
    RenderSection,
    TaskStatus,
)
from xiaowei_agent.rendering.model_advisory import append_model_advisory


def test_advisory_append_preserves_every_deterministic_field() -> None:
    base = RenderPayload(
        answer="找到 1 条慢查询。",
        sections=(RenderSection(title="findings", body="queryId=q-1", refs=()),),
        next_steps=("保留原下一步",),
        status=TaskStatus.SUCCEEDED,
        refs=("task-1:s1",),
    )
    advisory = ModelAdvisory(
        analysis="<script>alert(1)</script>",
        suggestions=("检查分区裁剪",),
        uncertainties=("尚未看到执行计划",),
    )

    rendered = append_model_advisory(base, advisory=advisory)

    assert rendered.answer == base.answer
    assert rendered.next_steps == base.next_steps
    assert rendered.status is base.status
    assert rendered.refs == base.refs
    assert rendered.sections[:-1] == base.sections
    assert len(rendered.sections) == 2
    assert rendered.sections[-1].title == "模型分析（仅供参考）"
    assert "<script>alert(1)</script>" in rendered.sections[-1].body
    assert rendered.sections[-1].refs == ()


def test_absent_advisory_returns_the_original_payload_unchanged() -> None:
    base = RenderPayload(
        answer="确定性回答",
        sections=(),
        next_steps=(),
        status=TaskStatus.SUCCEEDED,
        refs=(),
    )
    assert append_model_advisory(base, advisory=None) == base

"""尚未形成 ExecutionPlan 时的通用终态投影。"""

from typing import Final

from xiaowei_agent.contracts import (
    ClarificationPayload,
    ClarificationReasonCode,
    ClarificationRecord,
    RenderPayload,
    TaskStatus,
)

_PREPLAN_REJECTED: Final[str] = "请求在执行前被拒绝，未调用任何工具。"


def render_preplan_rejection(*, status: TaskStatus) -> RenderPayload:
    """只投影确定性拒绝；没有计划时不得猜测领域事实。"""
    if status is not TaskStatus.REJECTED:
        raise ValueError("generic pre-plan projection requires rejected status")
    return RenderPayload(
        answer=_PREPLAN_REJECTED,
        sections=(),
        next_steps=(),
        status=status,
        refs=(),
    )


_CLARIFICATION_PROMPTS: Final[dict[ClarificationReasonCode, str]] = {
    ClarificationReasonCode.INTERACTION_KIND_AMBIGUOUS: (
        "我还需要确认这次请求的类型，请补充更明确的运维目标。"
    ),
    ClarificationReasonCode.INTERACTION_ENVIRONMENT_ASSERTION_UNCLEAR: (
        "我还需要确认目标环境，请补充明确的环境信息。"
    ),
    ClarificationReasonCode.CAPABILITY_FIELDS_MISSING: "请补充缺失字段后重新提交。",
    ClarificationReasonCode.CAPABILITY_FIELDS_AMBIGUOUS: (
        "有些字段不够明确，请补充更精确的信息。"
    ),
    ClarificationReasonCode.CAPABILITY_ASSET_SELECTOR_REQUIRED: (
        "请补充一个明确的资产选择条件。"
    ),
}


def render_clarification_payload(*, record: ClarificationRecord) -> ClarificationPayload:
    """只从持久化 ClarificationRecord 投影澄清提示。"""
    prompt = _CLARIFICATION_PROMPTS[record.reason_code]
    if record.subject.kind == "capability" and record.missing_fields:
        fields = ", ".join(field.value for field in record.missing_fields)
        prompt = f"{prompt} 缺失字段：{fields}。"
    return ClarificationPayload(
        reason_code=record.reason_code,
        missing_fields=record.missing_fields,
        confirmed_slots=record.confirmed_slots,
        prompt=prompt,
    )


def render_clarification_as_payload(
    *, clarification: ClarificationPayload
) -> RenderPayload:
    """兼容同步 ``handle()`` 的 RenderPayload 形态。"""
    return RenderPayload(
        answer=clarification.prompt,
        sections=(),
        next_steps=(),
        status=TaskStatus.CLARIFICATION_REQUIRED,
        refs=(),
    )

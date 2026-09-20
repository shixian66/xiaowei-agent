"""尚未形成 ExecutionPlan 时的通用终态投影。"""

from typing import Final

from xiaowei_agent.contracts import (
    CapabilitySnapshot,
    CapabilitySpec,
    ClarificationPayload,
    ClarificationReasonCode,
    ClarificationRecord,
    RenderPayload,
    RenderSection,
    TaskStatus,
)

_PREPLAN_REJECTED: Final[str] = "请求在执行前被拒绝，未调用任何工具。"
CONVERSATION_TERMINAL_REASON: Final[str] = "interaction.conversation_responded"
_CONVERSATION_ANSWER: Final[str] = (
    "我能做的事就是下面这份能力清单，它直接来自当前能力快照，不是我总结出来的。"
    "这个普通对话通道本身不调用工具、不访问外部系统、不读取历史，也不会把聊天内容"
    "当成澄清父链或审批。"
)
_CONVERSATION_EMPTY_ANSWER: Final[str] = (
    "当前能力快照里没有任何已注册能力，所以我现在无法执行任何运维动作。"
    "这个普通对话通道本身不调用工具、不访问外部系统、不读取历史。"
)
_CONVERSATION_NEXT_STEPS: Final[tuple[str, ...]] = (
    "如果需要执行诊断，请提交明确的运维目标、环境和时间范围。",
)
_SNAPSHOT_REF_PREFIX: Final[str] = "capability-snapshot:"
_CAPABILITY_REF_PREFIX: Final[str] = "capability:"


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


def _capability_section(spec: CapabilitySpec) -> RenderSection:
    """把一条能力声明投影成一节；只重排声明字段，不加任何解释性断言。"""
    operations = "；".join(
        f"{operation.operation}（{operation.effect_class.value}"
        + (
            ""
            if operation.read_class is None
            else f"/{operation.read_class.value}"
        )
        + f"，经 {operation.gateway}）"
        for operation in spec.operations
    )
    return RenderSection(
        title=spec.capability_id,
        body=f"领域 {spec.domain}，版本 {spec.version}。可用操作：{operations}。",
        refs=(f"{_CAPABILITY_REF_PREFIX}{spec.capability_id}@{spec.version}",),
    )


def render_conversation_response(*, snapshot: CapabilitySnapshot) -> RenderPayload:
    """I2-B 限定普通对话的确定性投影：回答就是当前能力快照本身。

    **答案只由 ``snapshot`` 决定**，与用户文本无关。这既是"限定领域"的含义，也是
    这条通道唯一安全的形状：任何让用户文本参与生成的做法，都会把一个不调用工具的
    通道变成可被注入的自由问答口。

    投影的是**当前**快照而不是任务创建时的快照——"你能做什么"问的就是此刻的事实，
    这与证据投影必须钉死在已读到的东西上正好相反。``refs`` 带上 ``snapshot_id``，
    所以读者永远能分辨这份回答出自哪一份声明。
    """
    return RenderPayload(
        answer=(
            _CONVERSATION_ANSWER if snapshot.specs else _CONVERSATION_EMPTY_ANSWER
        ),
        sections=tuple(_capability_section(spec) for spec in snapshot.specs),
        next_steps=_CONVERSATION_NEXT_STEPS,
        status=TaskStatus.SUCCEEDED,
        refs=(f"{_SNAPSHOT_REF_PREFIX}{snapshot.snapshot_id}",),
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

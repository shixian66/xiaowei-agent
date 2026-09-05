"""所有 capability 共用的审批暂停投影。"""

from typing import Final

from xiaowei_agent.contracts import EvidenceEnvelope, RenderPayload, RenderSection, TaskStatus

_ANSWER_PENDING: Final[str] = "该步骤需要审批，任务已暂停等待人工决定。"
_NEXT_STEP_APPROVE: Final[str] = "请审批人处理该审批请求后恢复任务。"


def render_pending(
    *, approval_ref: str, evidences: tuple[EvidenceEnvelope, ...]
) -> RenderPayload:
    """保留已取得证据引用，但不猜测任何领域结论。"""
    limitations = tuple(
        dict.fromkeys(item for envelope in evidences for item in envelope.limitations)
    )
    sections = (
        ()
        if not limitations
        else (
            RenderSection(
                title="limitations",
                body="\n".join(limitations),
                refs=tuple(envelope.evidence_id for envelope in evidences),
            ),
        )
    )
    return RenderPayload(
        answer=_ANSWER_PENDING,
        sections=sections,
        next_steps=(_NEXT_STEP_APPROVE,),
        status=TaskStatus.AWAITING_APPROVAL,
        refs=(*(envelope.evidence_id for envelope in evidences), approval_ref),
    )

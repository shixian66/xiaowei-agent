"""慢查询回答的投影。

**不输出**：完整 SQL 原文、``stmt`` 列、表名、secret、连接串、伪确定性根因。
方向判断一律带"疑似"，且只进说明段，不进事实。
"""

from typing import Final

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    FrozenMap,
    RenderPayload,
    RenderSection,
    TaskStatus,
)

_ANSWER_FOUND: Final[str] = "在该范围内找到 {count} 条慢查询。"
_ANSWER_NONE: Final[str] = "该范围内有查询流量，但没有超过阈值的慢查询。"
_ANSWER_INDETERMINATE: Final[str] = (
    "无法确认该范围内的慢查询情况：证据不足，结果按不确定处理。"
)
_ANSWER_PENDING: Final[str] = "该步骤需要审批，任务已暂停等待人工决定。"

_SECTION_LIMITATIONS: Final[str] = "limitations"
_SECTION_FINDINGS: Final[str] = "findings"
_SECTION_MISSING: Final[str] = "missing"

_NEXT_STEP_CHECK_SCOPE: Final[str] = (
    "确认库名、用户名与时间窗是否正确；范围写错时审计表里本就不会有数据。"
)
_NEXT_STEP_CHECK_PIPELINE: Final[str] = (
    "确认审计采集是否正常：范围内完全没有审计行，也可能是采集中断。"
)
_NEXT_STEP_APPROVE: Final[str] = "请审批人处理该审批请求后恢复任务。"

# 方向判断的阈值来自旧项目，**未在本项目的真实工作负载上校准**，因此结论一律带
# "疑似"，且只进说明段、不进事实。
_HIGH_SCAN_ROWS: Final[int] = 10_000_000


def _rows(evidences: tuple[EvidenceEnvelope, ...]) -> tuple[FrozenMap, ...]:
    for envelope in evidences:
        if envelope.evidence_id.endswith(":s1"):
            return envelope.facts
    return ()


def _limitation_section(evidences: tuple[EvidenceEnvelope, ...]) -> RenderSection | None:
    lines = [item for envelope in evidences for item in envelope.limitations]
    if not lines:
        return None
    return RenderSection(
        title=_SECTION_LIMITATIONS,
        # 限制文案本身就是"可复现参数"：窗口、目标范围、阈值、行数上限。
        body="\n".join(dict.fromkeys(lines)),
        refs=tuple(envelope.evidence_id for envelope in evidences),
    )


def _findings_section(rows: tuple[FrozenMap, ...]) -> RenderSection | None:
    if not rows:
        return None
    lines: list[str] = []
    for row in rows:
        query_id = row.get("queryId")
        query_time = row.get("queryTime")
        scan_rows = row.get("scanRows")
        line = f"queryId={query_id} queryTime_ms={query_time} scanRows={scan_rows}"
        if isinstance(scan_rows, int) and scan_rows >= _HIGH_SCAN_ROWS:
            line += "（疑似扫描行数过大，阈值未在本项目校准）"
        lines.append(line)
    return RenderSection(title=_SECTION_FINDINGS, body="\n".join(lines), refs=())


def _missing_section(verdict: AnswerabilityVerdict) -> RenderSection | None:
    if not verdict.missing:
        return None
    return RenderSection(
        title=_SECTION_MISSING,
        body="\n".join(f"{item.key}: {item.reason_key}" for item in verdict.missing),
        refs=(),
    )


def render(
    *,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    status: TaskStatus,
) -> RenderPayload:
    """把证据与可答性结论投影成回答。

    :param status: 由 Runtime 依 ``terminal_status_for`` 确定性判定的终态；渲染
        **不重算**它——渠道与投影层都不拥有业务判断。
    """
    rows = _rows(evidences)
    if status is not TaskStatus.SUCCEEDED:
        answer = _ANSWER_INDETERMINATE
        next_steps: tuple[str, ...] = (_NEXT_STEP_CHECK_SCOPE, _NEXT_STEP_CHECK_PIPELINE)
    elif rows:
        answer = _ANSWER_FOUND.format(count=len(rows))
        next_steps = ()
    else:
        answer = _ANSWER_NONE
        next_steps = ()
    sections = tuple(
        section
        for section in (
            _findings_section(rows),
            _missing_section(verdict),
            _limitation_section(evidences),
        )
        if section is not None
    )
    return RenderPayload(
        answer=answer,
        sections=sections,
        next_steps=next_steps,
        status=status,
        refs=tuple(envelope.evidence_id for envelope in evidences),
    )


def render_pending(
    *, approval_ref: str, evidences: tuple[EvidenceEnvelope, ...]
) -> RenderPayload:
    """暂停时的可审计 pending 投影。

    ``refs`` 同时携带审批引用与**已取得的**证据引用：暂停不是"什么都没发生"，
    已经取到的事实必须仍然可追溯。
    """
    section = _limitation_section(evidences)
    return RenderPayload(
        answer=_ANSWER_PENDING,
        sections=() if section is None else (section,),
        next_steps=(_NEXT_STEP_APPROVE,),
        status=TaskStatus.AWAITING_APPROVAL,
        refs=(*(envelope.evidence_id for envelope in evidences), approval_ref),
    )

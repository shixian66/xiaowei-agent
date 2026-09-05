"""Prometheus 告警与指标证据的统一回答投影。"""

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    FrozenMap,
    RenderPayload,
    RenderSection,
    TaskStatus,
)


def _facts(
    evidences: tuple[EvidenceEnvelope, ...], suffix: str
) -> tuple[FrozenMap, ...]:
    for evidence in evidences:
        if evidence.evidence_id.endswith(suffix):
            return evidence.facts
    return ()


def _alert_section(rows: tuple[FrozenMap, ...]) -> RenderSection | None:
    if not rows:
        return None
    lines = [
        (
            f"fingerprint={row.get('fingerprint')} state={row.get('state')} "
            f"alert={row.get('alert_name')} instance={row.get('instance')} "
            f"severity={row.get('severity')} starts_at={row.get('starts_at')} "
            f"ends_at={row.get('ends_at')} silenced={row.get('silenced')} "
            f"inhibited={row.get('inhibited')}"
        )
        for row in rows
    ]
    return RenderSection(title="active alerts", body="\n".join(lines), refs=())


def _metric_section(rows: tuple[FrozenMap, ...]) -> RenderSection | None:
    if not rows:
        return None
    lines = [
        (
            f"metric={row.get('metric_name')} instance={row.get('instance')} "
            f"points={row.get('point_count')} first={row.get('first_value')} "
            f"latest={row.get('latest_value')} min={row.get('min_value')} "
            f"max={row.get('max_value')} delta={row.get('delta')} "
            f"trend={row.get('trend')}"
        )
        for row in rows
    ]
    return RenderSection(title="metric evidence", body="\n".join(lines), refs=())


def _missing_section(verdict: AnswerabilityVerdict) -> RenderSection | None:
    if not verdict.missing:
        return None
    return RenderSection(
        title="missing",
        body="\n".join(
            f"{item.key}: {item.reason_key}" for item in verdict.missing
        ),
        refs=(),
    )


def _limitations_section(
    evidences: tuple[EvidenceEnvelope, ...], verdict: AnswerabilityVerdict
) -> RenderSection | None:
    limitations = tuple(dict.fromkeys(verdict.limitations))
    if not limitations:
        return None
    return RenderSection(
        title="limitations",
        body="\n".join(limitations),
        refs=tuple(item.evidence_id for item in evidences),
    )


def render_prometheus_alert(
    *,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    status: TaskStatus,
) -> RenderPayload:
    """展示原始状态与指标摘要；明确不输出自动根因结论。"""
    alerts = _facts(evidences, ":s1")
    metrics = _facts(evidences, ":s2")
    if status is TaskStatus.SUCCEEDED:
        answer = "已取得唯一告警与对应指标证据；这是事实汇总，不是自动根因结论。"
        next_steps: tuple[str, ...] = ()
    else:
        answer = "告警或指标证据不足，当前结果按不确定处理；不是自动根因结论。"
        next_steps = (
            ("请提供 fingerprint 以消歧。",)
            if verdict.needs_user_input
            else ("请核对告警名、instance、时间窗及监控数据可用性。",)
        )
    sections = tuple(
        section
        for section in (
            _alert_section(alerts),
            _metric_section(metrics),
            _missing_section(verdict),
            _limitations_section(evidences, verdict),
        )
        if section is not None
    )
    return RenderPayload(
        answer=answer,
        sections=sections,
        next_steps=next_steps,
        status=status,
        refs=tuple(item.evidence_id for item in evidences),
    )

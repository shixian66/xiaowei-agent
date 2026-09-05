"""Prometheus 告警证据的确定性可答性判断。"""

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    MissingItem,
)


def _find(
    evidences: tuple[EvidenceEnvelope, ...], suffix: str
) -> EvidenceEnvelope | None:
    return next(
        (item for item in evidences if item.evidence_id.endswith(suffix)), None
    )


def assess_prometheus_alert(
    *, evidences: tuple[EvidenceEnvelope, ...]
) -> AnswerabilityVerdict:
    """只有唯一告警事实与至少一个有效指标 series 同时存在才充分。"""
    limitations = tuple(
        dict.fromkeys(
            limitation
            for evidence in evidences
            for limitation in evidence.limitations
        )
    )
    alerts = _find(evidences, ":s1")
    metrics = _find(evidences, ":s2")
    if alerts is None:
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=(*limitations, "active alert evidence is absent"),
            missing=(
                MissingItem(key="active_alert", reason_key="evidence.absent"),
            ),
            downgrade_suggestion=True,
            needs_user_input=False,
        )
    if len(alerts.facts) > 1:
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=(*limitations, "multiple active alerts matched"),
            missing=(
                MissingItem(key="fingerprint", reason_key="evidence.ambiguous"),
            ),
            downgrade_suggestion=True,
            needs_user_input=True,
        )
    if not alerts.facts:
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=(*limitations, "no active alert matched the exact filter"),
            missing=(MissingItem(key="active_alert", reason_key="evidence.empty"),),
            downgrade_suggestion=True,
            needs_user_input=False,
        )
    if metrics is None or not metrics.facts:
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=(*limitations, "metric evidence is absent or empty"),
            missing=(
                MissingItem(key="metric_series", reason_key="evidence.absent"),
            ),
            downgrade_suggestion=True,
            needs_user_input=False,
        )
    return AnswerabilityVerdict(
        sufficient=True,
        limitations=limitations,
        missing=(),
        downgrade_suggestion=False,
        needs_user_input=False,
    )

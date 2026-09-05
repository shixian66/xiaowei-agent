"""Prometheus 告警与指标结果的纯证据构造器。"""

import datetime as dt
import math
from collections import defaultdict
from typing import Final

from xiaowei_agent.contracts import (
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalSource,
    FrozenMap,
    JsonScalar,
    PlanStep,
    ToolResult,
)
from xiaowei_agent.contracts.evidence import evidence_id
from xiaowei_agent.evidence.errors import EvidenceBuildError

_ALERT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "fingerprint",
        "state",
        "alert_name",
        "instance",
        "severity",
        "starts_at",
        "ends_at",
        "silenced",
        "inhibited",
    }
)
_METRIC_FIELDS: Final[frozenset[str]] = frozenset(
    {"series_key", "metric_name", "instance", "timestamp_ms", "value"}
)


def _aware_iso(value: JsonScalar) -> str:
    if not isinstance(value, str):
        raise EvidenceBuildError
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise EvidenceBuildError from None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise EvidenceBuildError
    return parsed.astimezone(dt.UTC).isoformat()


def _alert_fact(
    row: FrozenMap,
    *,
    expected_alert_name: str,
    expected_instance: str,
    expected_fingerprint: str | None,
) -> dict[str, JsonScalar]:
    if not _ALERT_FIELDS <= set(row):
        raise EvidenceBuildError
    strings = ("fingerprint", "state", "alert_name", "instance", "severity")
    if any(not isinstance(row.get(key), str) for key in strings):
        raise EvidenceBuildError
    if row["alert_name"] != expected_alert_name or row["instance"] != expected_instance:
        raise EvidenceBuildError
    if expected_fingerprint is not None and row["fingerprint"] != expected_fingerprint:
        raise EvidenceBuildError
    if type(row["silenced"]) is not bool or type(row["inhibited"]) is not bool:
        raise EvidenceBuildError
    ends_at = row["ends_at"]
    canonical_end = None if ends_at is None else _aware_iso(ends_at)
    return {
        "fingerprint": row["fingerprint"],
        "state": row["state"],
        "alert_name": row["alert_name"],
        "instance": row["instance"],
        "severity": row["severity"],
        "starts_at": _aware_iso(row["starts_at"]),
        "ends_at": canonical_end,
        "silenced": row["silenced"],
        "inhibited": row["inhibited"],
    }


def build_alert_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    result: ToolResult,
    captured_at: dt.datetime,
    expected_alert_name: str,
    expected_instance: str,
    expected_fingerprint: str | None,
    row_limit: int,
) -> EvidenceEnvelope:
    """过滤并验证 Alertmanager 扁平行，不自动选择多个告警。"""
    if row_limit <= 0 or len(result.data_view) > row_limit:
        raise EvidenceBuildError
    facts = tuple(
        sorted(
            (
                _alert_fact(
                    row,
                    expected_alert_name=expected_alert_name,
                    expected_instance=expected_instance,
                    expected_fingerprint=expected_fingerprint,
                )
                for row in result.data_view
            ),
            key=lambda row: str(row["fingerprint"]),
        )
    )
    fingerprints = tuple(row["fingerprint"] for row in facts)
    if len(set(fingerprints)) != len(fingerprints):
        raise EvidenceBuildError
    return EvidenceEnvelope(
        evidence_id=evidence_id(task_id=task_id, step_id=step.step_id),
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        facts=facts,
        source=result.source,
        source_kind=ExternalSource.TOOL,
        captured_at=captured_at,
        sampled=len(facts) == row_limit,
        limitations=(
            f"alert filter: {expected_alert_name} @ {expected_instance}",
            f"alert row limit: {row_limit}",
            f"returned alerts: {len(facts)}",
            "fields limited to evidence.prometheus.alert.v1",
        ),
        redaction_ref=None,
    )


def _finite_value(value: JsonScalar) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvidenceBuildError
    converted = float(value)
    if not math.isfinite(converted):
        raise EvidenceBuildError
    return converted


def _rounded(value: float) -> float:
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


def build_metric_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    result: ToolResult,
    captured_at: dt.datetime,
    expected_instance: str,
    expected_metric_name: str,
    template_id: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    max_series: int,
    max_points_per_series: int,
) -> EvidenceEnvelope:
    """校验扁平点并按 series_key 生成确定性摘要。"""
    if max_series <= 0 or max_points_per_series <= 0:
        raise EvidenceBuildError
    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)
    for row in result.data_view:
        if not _METRIC_FIELDS <= set(row):
            raise EvidenceBuildError
        series_key = row["series_key"]
        metric_name = row["metric_name"]
        instance = row["instance"]
        timestamp_ms = row["timestamp_ms"]
        if (
            not isinstance(series_key, str)
            or metric_name != expected_metric_name
            or instance != expected_instance
            or type(timestamp_ms) is not int
            or not start_ms <= timestamp_ms <= end_ms
        ):
            raise EvidenceBuildError
        points = grouped[series_key]
        if points and timestamp_ms <= points[-1][0]:
            raise EvidenceBuildError
        points.append((timestamp_ms, _finite_value(row["value"])))
        if len(points) > max_points_per_series:
            raise EvidenceBuildError
    if len(grouped) > max_series:
        raise EvidenceBuildError

    facts: list[dict[str, JsonScalar]] = []
    for series_key in sorted(grouped):
        values = [value for _timestamp, value in grouped[series_key]]
        delta = _rounded(values[-1] - values[0])
        trend = "rising" if delta > 0 else "falling" if delta < 0 else "flat"
        facts.append(
            {
                "metric_name": expected_metric_name,
                "instance": expected_instance,
                "point_count": len(values),
                "first_value": _rounded(values[0]),
                "latest_value": _rounded(values[-1]),
                "min_value": _rounded(min(values)),
                "max_value": _rounded(max(values)),
                "delta": delta,
                "trend": trend,
            }
        )
    sampled = len(grouped) == max_series or any(
        len(points) == max_points_per_series for points in grouped.values()
    )
    return EvidenceEnvelope(
        evidence_id=evidence_id(task_id=task_id, step_id=step.step_id),
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        facts=tuple(facts),
        source=result.source,
        source_kind=ExternalSource.TOOL,
        captured_at=captured_at,
        sampled=sampled,
        limitations=(
            f"window: [{window_start.isoformat()}, {window_end.isoformat()}]",
            f"PromQL template: {template_id}",
            f"series limit: {max_series}",
            f"points per series limit: {max_points_per_series}",
            f"returned series: {len(facts)}",
            "metric values are evidence, not an automatic causal diagnosis",
        ),
        redaction_ref=None,
    )

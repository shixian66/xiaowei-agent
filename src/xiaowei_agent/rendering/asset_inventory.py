"""资产精确查询的统一回答投影。"""

from typing import Final

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    FrozenMap,
    RenderPayload,
    RenderSection,
    TaskStatus,
)

_FACT_FIELDS: Final[tuple[str, ...]] = (
    "asset_id",
    "hostname",
    "primary_ip",
    "asset_type",
    "status",
    "owner_team",
    "service",
    "os_family",
    "environment_id",
)


def _asset_rows(evidences: tuple[EvidenceEnvelope, ...]) -> tuple[FrozenMap, ...]:
    matches = tuple(
        evidence.facts
        for evidence in evidences
        if evidence.evidence_id.endswith(":s1")
    )
    return matches[0] if len(matches) == 1 else ()


def _asset_section(rows: tuple[FrozenMap, ...]) -> RenderSection | None:
    if len(rows) != 1 or not set(_FACT_FIELDS) <= set(rows[0]):
        return None
    row = rows[0]
    if any(not isinstance(row.get(key), str) for key in _FACT_FIELDS):
        return None
    body = (
        f"asset_id={row['asset_id']} hostname={row['hostname']} "
        f"primary_ip={row['primary_ip']} asset_type={row['asset_type']} "
        f"status={row['status']} owner_team={row['owner_team']} "
        f"service={row['service']} os_family={row['os_family']} "
        f"environment_id={row['environment_id']}"
    )
    return RenderSection(title="asset summary", body=body, refs=())


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
        refs=tuple(evidence.evidence_id for evidence in evidences),
    )


def render_asset_inventory(
    *,
    evidences: tuple[EvidenceEnvelope, ...],
    verdict: AnswerabilityVerdict,
    status: TaskStatus,
) -> RenderPayload:
    """只展示唯一资产的九字段目录事实。"""
    rows = _asset_rows(evidences)
    reasons = {item.reason_key for item in verdict.missing}
    successful = status is TaskStatus.SUCCEEDED and verdict.sufficient
    if successful:
        answer = "已找到唯一资产的目录事实。"
        next_steps: tuple[str, ...] = ()
    elif "evidence.empty" in reasons:
        answer = "当前 scope 内未找到与精确 selector 匹配的资产。"
        next_steps = ("请核对精确 selector 与 tenant/environment scope。",)
    elif "evidence.ambiguous" in reasons:
        answer = "资产目录返回多条精确匹配，当前结果按不确定处理。"
        next_steps = ("请修正资产目录中的精确标识唯一性。",)
    else:
        answer = "资产目录证据不足，当前结果按不确定处理。"
        next_steps = ("请核对精确 selector、scope 与资产目录可用性。",)
    sections = tuple(
        section
        for section in (
            _asset_section(rows) if successful else None,
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
        refs=tuple(evidence.evidence_id for evidence in evidences),
    )

"""资产精确查询证据的确定性可答性判断。"""

from typing import Final

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    MissingItem,
)

_CAPABILITY_KEY: Final[tuple[str, str]] = ("asset.inventory.lookup", "1.0.0")


def _limitations(evidences: tuple[EvidenceEnvelope, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            limitation
            for evidence in evidences
            for limitation in evidence.limitations
        )
    )


def _insufficient(
    *, limitations: tuple[str, ...], key: str, reason_key: str
) -> AnswerabilityVerdict:
    return AnswerabilityVerdict(
        sufficient=False,
        limitations=limitations,
        missing=(MissingItem(key=key, reason_key=reason_key),),
        downgrade_suggestion=True,
        needs_user_input=False,
    )


def assess_asset_inventory(
    *, evidences: tuple[EvidenceEnvelope, ...]
) -> AnswerabilityVerdict:
    """只有一个 scope 一致的资产事实才足以回答。"""
    limitations = _limitations(evidences)
    if any(
        (evidence.capability_id, evidence.capability_version) != _CAPABILITY_KEY
        for evidence in evidences
    ):
        return _insufficient(
            limitations=(*limitations, "asset evidence capability key mismatch"),
            key="asset_evidence",
            reason_key="evidence.capability_mismatch",
        )
    matching = tuple(
        evidence for evidence in evidences if evidence.evidence_id.endswith(":s1")
    )
    if len(matching) != 1:
        return _insufficient(
            limitations=(*limitations, "asset lookup evidence is absent"),
            key="asset",
            reason_key="evidence.absent",
        )
    row_count = len(matching[0].facts)
    if row_count == 0:
        return _insufficient(
            limitations=(*limitations, "no asset matched the exact selector and scope"),
            key="asset",
            reason_key="evidence.empty",
        )
    if row_count != 1:
        return _insufficient(
            limitations=(*limitations, "multiple assets matched the exact selector"),
            key="unique_asset",
            reason_key="evidence.ambiguous",
        )
    return AnswerabilityVerdict(
        sufficient=True,
        limitations=limitations,
        missing=(),
        downgrade_suggestion=False,
        needs_user_input=False,
    )

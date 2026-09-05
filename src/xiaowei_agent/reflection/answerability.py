"""可答性判断：只读消费已生成的证据，产出**建议性**结论。

判定表（"范围"= 计划里的目标范围，即时间窗 + 全部已给定的目标过滤）：

| s1 结果 | s2 结果        | sufficient | downgrade | Runtime 终态 |
| ------- | -------------- | ---------- | --------- | ------------ |
| 有行    | 未执行         | True       | False     | SUCCEEDED    |
| 空      | 范围内 count>0 | True       | False     | SUCCEEDED    |
| 空      | 范围内 count=0 | False      | True      | INDETERMINATE|
| 空      | 未执行/失败    | False      | True      | INDETERMINATE|
| 无证据  | 任意           | False      | True      | INDETERMINATE|

**不使用 LLM**：安全、权限、SQL 形状、审批绑定与终态一律由确定性断言验收
（ARCHITECTURE §13.2）。

**本模块不拥有执行权**：``assess`` 的入参只有证据——没有 plan、target、gateway 或
store，越权在**入参上就不可表达**。是否真的进入 ``indeterminate`` 由 Runtime 依
``terminal_status_for`` 确定性决定并写 TaskStore。
"""

from typing import Final

from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    MissingItem,
)
from xiaowei_agent.reflection.status import terminal_status_for as terminal_status_for

COUNT_ALIAS: Final[str] = "query_count"
LIST_STEP_SUFFIX: Final[str] = ":s1"
COUNT_STEP_SUFFIX: Final[str] = ":s2"

_NO_EVIDENCE: Final[str] = "no evidence was collected for this request"
_NO_AUDIT_DATA: Final[str] = (
    "the target scope has no audit rows in this window; "
    "this is not the same as having no slow queries"
)
_SCOPE_UNCONFIRMED: Final[str] = (
    "the optional scope check did not run, so an empty result cannot be interpreted"
)
_SAMPLED: Final[str] = "row limit reached; the result may be truncated (sampled)"


def _find(evidences: tuple[EvidenceEnvelope, ...], suffix: str) -> EvidenceEnvelope | None:
    for envelope in evidences:
        if envelope.evidence_id.endswith(suffix):
            return envelope
    return None


def _scope_row_count(envelope: EvidenceEnvelope) -> int | None:
    """从 count 证据里读出范围内的行数；读不出即 ``None``（fail-closed）。"""
    for row in envelope.facts:
        value = row.get(COUNT_ALIAS)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def assess(*, evidences: tuple[EvidenceEnvelope, ...]) -> AnswerabilityVerdict:
    """依据已收集的证据判断是否足以回答。

    :param evidences: 从 ``EvidenceLedger`` 读回的证据，按写入顺序。
    :returns: 建议性结论；它不设置终态，也不写任何存储。
    """
    limitations: list[str] = []
    missing: list[MissingItem] = []
    for envelope in evidences:
        limitations.extend(envelope.limitations)
        if envelope.sampled:
            limitations.append(_SAMPLED)

    listing = _find(evidences, LIST_STEP_SUFFIX)
    if listing is None:
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=(*limitations, _NO_EVIDENCE),
            missing=(
                MissingItem(key="slow_query_rows", reason_key="evidence.absent"),
            ),
            downgrade_suggestion=True,
            needs_user_input=False,
        )

    if listing.facts:
        # 取到了慢查询明细，这本身就是答案。
        return AnswerabilityVerdict(
            sufficient=True,
            limitations=tuple(limitations),
            missing=(),
            downgrade_suggestion=False,
            needs_user_input=False,
        )

    counted = _find(evidences, COUNT_STEP_SUFFIX)
    scope_rows = None if counted is None else _scope_row_count(counted)
    if scope_rows is None:
        # 可选分支未执行或结果不可解读：无法区分"范围内没有慢查询"与"拿不到
        # 该范围的审计数据"，因此不能给出自信的答案。
        missing.append(MissingItem(key="scope_row_count", reason_key="evidence.absent"))
        return AnswerabilityVerdict(
            sufficient=False,
            limitations=(*limitations, _SCOPE_UNCONFIRMED),
            missing=tuple(missing),
            downgrade_suggestion=True,
            needs_user_input=False,
        )
    if scope_rows > 0:
        # 范围内有查询但无慢查询——一个确定的答案，不是失败。
        return AnswerabilityVerdict(
            sufficient=True,
            limitations=tuple(limitations),
            missing=(),
            downgrade_suggestion=False,
            needs_user_input=False,
        )
    # 范围内没有任何审计数据：采集断了 / 库名或用户写错 / 权限受限。
    missing.append(MissingItem(key="audit_rows_in_scope", reason_key="evidence.empty"))
    return AnswerabilityVerdict(
        sufficient=False,
        limitations=(*limitations, _NO_AUDIT_DATA),
        missing=tuple(missing),
        downgrade_suggestion=True,
        # 库名或用户可能写错，补充信息可能让下一次取到数据。
        needs_user_input=True,
    )

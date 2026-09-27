"""F1-0：设计 §13 要求的真源修订必须落在各自真源里，且不复活“只接受模板 SQL”的旧口径。"""

from pathlib import Path
from typing import Final

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ADR = "docs/adr/"
_ADR_007 = _ADR + "ADR-007-first-capabilities-execution-context-and-live-call-authorization.md"
_ADR_009 = _ADR + "ADR-009-plan-hash-approval-binding-and-tool-admission.md"
_ADR_012 = _ADR + "ADR-012-m6b-target-bound-starrocks-readonly-adapter.md"
_ADR_013 = _ADR + "ADR-013-m7-channel-boundary.md"
_ADR_017 = _ADR + "ADR-017-intelligent-interaction-and-clarification.md"
_ADR_018 = _ADR + "ADR-018-f1-sql-and-result-artifacts.md"
_PLAN = "docs/superpowers/plans/2026-09-27-f1-starrocks-readonly-query.md"
_ROADMAP = "docs/superpowers/specs/2026-09-26-feature-roadmap-direction.md"
_WEB_SPEC = "docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md"

# 每个真源必须承载的 F1 决定；键是文件，值是该文件独有的承重术语。
_F1_TRUTH_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "AGENTS.md": (
        "模板 SQL 由确定性 compiler 生成",
        "用户直接 SQL 只能来自受保护 SQL artifact",
        "执行 SQL 不来自模型原文",
    ),
    "ARCHITECTURE.md": (
        "F1 显式 SQL artifact 入口",
        "ArtifactSubmission",
        "TargetQueryLeaseStore",
        "ThreadsafeResultChunkWriter",
        "`application/sql` 草稿路由固定 1 MiB 硬上限",
        "PREVIOUS_ATTEMPT_UNCERTAIN",
        "schedule_deferral",
        "ReadonlyStatementRegistry",
        "READONLY_STATEMENT_NOT_SUPPORTED",
        "**不提供元数据保密**",
        "ADR-018",
    ),
    _ADR_007: ("`starrocks.readonly_query`", "F1 授权矩阵", "F1-H"),
    _ADR_009: (
        "`confirmed_artifact`",
        "HydratedQuery",
        "`ToolCall.typed_args` 继续只接受 JSON 标量",
        "`begin_step_attempt`",
    ),
    _ADR_012: (
        "ResourceSnapshot",
        "`verified_min_version <= actual_version <= verified_max_version`",
        "`read_timeout=Q+10`",
        "`SSCursor.close()`",
    ),
    _ADR_013: ("`/results/{result_ref}`", "不嵌入 SQL 原文、列名或结果行"),
    _ADR_017: ("`RESTRICTED` 不等于必须审批", "`confirmed_readonly`"),
    _ADR_018: (
        "`staging`、`sealed`、`available`、`failed`、`expired`",
        "`requester_owner`",
        "`export_policy` 在 F1 固定为 `disabled`",
    ),
    _PLAN: (
        "F1-0a",
        "F1-0b",
        "首批 ReadonlyStatementRegistry 清单",
        "Web 规格 §11.2 六项对照",
    ),
    _ROADMAP: ("Web 显式 SQL 模式下用户提交并完整确认的受保护 SQL artifact",),
    _WEB_SPEC: ("F1 锁定结果页不写 approver grant",),
}

# 设计 §13 第 6 项要求移除的过期口径。
_STALE_F1_PHRASES: Final[dict[str, tuple[str, ...]]] = {
    "AGENTS.md": ("SQL 必须确定性生成并经 AST 校验",),
    "ARCHITECTURE.md": ("允许执行的 SQL 由确定性 compiler 生成；",),
    "DEVELOPMENT_PLAN.md": ("确定性 SQL + AST；",),
    _ROADMAP: ("确定性 SQL 模板与 AST 校验，不接受用户或模型直接提供的可执行 SQL",),
}


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _missing_terms(docs: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (name, term)
        for name, terms in _F1_TRUTH_TERMS.items()
        for term in terms
        if term not in docs[name]
    ]


def _stale_phrases(docs: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (name, phrase)
        for name, phrases in _STALE_F1_PHRASES.items()
        for phrase in phrases
        if phrase in docs[name]
    ]


def _all_docs() -> dict[str, str]:
    names = set(_F1_TRUTH_TERMS) | set(_STALE_F1_PHRASES)
    return {name: _read(name) for name in names}


def test_every_f1_truth_source_carries_its_decisions() -> None:
    assert _missing_terms(_all_docs()) == []


def test_no_truth_source_revives_template_only_sql_wording() -> None:
    assert _stale_phrases(_all_docs()) == []


@pytest.mark.parametrize(
    "path",
    [_ADR_007, _ADR_009, _ADR_012, _ADR_013, _ADR_017, _ADR_018],
)
def test_f1_adr_changes_stay_proposed_until_owner_acceptance(path: str) -> None:
    # 负责人接受前，F1 修订只能是 Proposed，不能把文档改动写成已接受。
    text = _read(path)
    assert "Proposed" in text
    assert "接受前不得写 F1 行为源码" in text


def test_f1_truth_bindings_are_discriminating() -> None:
    docs = _all_docs()
    without_rule = dict(docs)
    without_rule["AGENTS.md"] = docs["AGENTS.md"].replace(
        "用户直接 SQL 只能来自受保护 SQL artifact", ""
    )
    assert ("AGENTS.md", "用户直接 SQL 只能来自受保护 SQL artifact") in _missing_terms(
        without_rule
    )
    revived = dict(docs)
    revived["DEVELOPMENT_PLAN.md"] = docs["DEVELOPMENT_PLAN.md"] + "\n确定性 SQL + AST；\n"
    assert _stale_phrases(revived) == [("DEVELOPMENT_PLAN.md", "确定性 SQL + AST；")]

"""F1-0：设计 v9 §13 要求的真源修订必须落在各自真源里，且不复活已删除的旧口径。"""

from pathlib import Path
from typing import Final

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ADR = "docs/adr/"
_ADR_007 = _ADR + "ADR-007-first-capabilities-execution-context-and-live-call-authorization.md"
_ADR_009 = _ADR + "ADR-009-plan-hash-approval-binding-and-tool-admission.md"
_ADR_010 = _ADR + "ADR-010-m5-durable-attempt-and-compose-boundary.md"
_ADR_012 = _ADR + "ADR-012-m6b-target-bound-starrocks-readonly-adapter.md"
_ADR_013 = _ADR + "ADR-013-m7-channel-boundary.md"
_ADR_017 = _ADR + "ADR-017-intelligent-interaction-and-clarification.md"
_ADR_018 = _ADR + "ADR-018-f1-sql-and-result-artifacts.md"
_SPEC = "docs/superpowers/specs/2026-09-27-f1-starrocks-readonly-query-design.md"
_PLAN = "docs/superpowers/plans/2026-09-27-f1-starrocks-readonly-query.md"
_ROADMAP = "docs/superpowers/specs/2026-09-26-feature-roadmap-direction.md"
_WEB_SPEC = "docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md"

# 每个真源必须承载的 F1 决定；键是文件，值是该文件的承重术语。
_F1_TRUTH_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "AGENTS.md": (
        "模板 SQL 由确定性 compiler 生成",
        "用户直接 SQL 只能来自受保护 SQL artifact",
        "模型输出本身永远不是可执行 SQL",
    ),
    "ARCHITECTURE.md": (
        "F1 SQL 查询能力",
        "recognize_sql_message",
        "CAPABILITY_TARGET_SELECTION_REQUIRED",
        "ArtifactSubmission",
        "submit_sql_query",
        "QueryResultBuffer",
        "worker_max_concurrent_tasks",
        "READONLY_STATEMENT_NOT_SUPPORTED",
        "**不提供元数据保密**",
        "ADR-018",
    ),
    _ADR_007: ("`starrocks.readonly_query`", "F1 授权矩阵", "F1-H"),
    _ADR_009: (
        "`confirmed_artifact`",
        "HydratedQuery",
        "`ToolCall.typed_args` 继续只接受 JSON 标量",
        "`BUDGET_EXHAUSTED`",
    ),
    _ADR_010: ("`worker_max_concurrent_tasks`，默认 4", "`run_with_task_heartbeat`"),
    _ADR_012: ("`read_timeout=Q+10`", "`SSCursor.close()`", "**不做** D5 的"),
    _ADR_013: (
        "`/results/{result_ref}`",
        "不嵌入 SQL 原文、列名或",
        "**飞书追问以引用回复作答**",
    ),
    _ADR_017: (
        "`RESTRICTED` 不等于必须审批",
        "`confirmed_readonly`",
        "模型来源不得产生 `starrocks_readonly_query` 意图",
        "该意图只接受\n`origin=rule`",
        "ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED",
        "不设默认目标",
    ),
    _ADR_018: (
        "模型不能创建",
        "结果只在步骤成功时产生",
        "`requester_owner`",
        "`export_policy` 在 F1 固定为 `disabled`",
        "`TaskStore.submit_sql_query` 是唯一的提交入口",
        "### D4 保留（不设配额）",
    ),
    _SPEC: (
        "Draft v9（Agent 主链集成版）",
        "65_536 bytes",
        "worker_max_concurrent_tasks",
        "这条 SQL **不交给模型**",
    ),
    _PLAN: ("F1-0a", "F1-0b", "首批只读语句清单", "Web 规格 §11.2 六项对照"),
    _ROADMAP: ("用户在网页、飞书直接发送并经确定性识别保存的受保护 SQL artifact",),
    _WEB_SPEC: ("F1 锁定结果页不写 approver grant",),
}

# 已移除的旧口径：模板 SQL 唯一来源、v7 专属机制、v8 的配额与 SQL 页面（负责人决定）。
_V7_ONLY_TERMS: Final[tuple[str, ...]] = (
    "TargetQueryLeaseStore",
    "schedule_deferral",
    "WorkflowDeferred",
    "ThreadsafeResultChunkWriter",
    "ResultChunkSink",
    "confirm_sql_artifact",
    "ReadonlyStatementRegistry",
    "verified_min_version",
    "application/sql",
    "inspect_step_execution",
    "存活 query set",
    "Web 显式 SQL",
)
# 只写现行口径的真源；设计 §2 修订记录与 ADR-018 备选方案会按名称引用 v7，不在此列。
_CURRENT_ONLY_DOCS: Final[tuple[str, ...]] = (
    "AGENTS.md",
    "ARCHITECTURE.md",
    _ADR_007,
    _ADR_009,
    _ADR_010,
    _ADR_012,
    _ADR_013,
    _ADR_017,
    _PLAN,
)
_STALE_F1_PHRASES: Final[dict[str, tuple[str, ...]]] = {
    "AGENTS.md": ("SQL 必须确定性生成并经 AST 校验",),
    "ARCHITECTURE.md": ("允许执行的 SQL 由确定性 compiler 生成；",),
    "DEVELOPMENT_PLAN.md": ("确定性 SQL + AST；",),
    _ROADMAP: ("确定性 SQL 模板与 AST 校验，不接受用户或模型直接提供的可执行 SQL",),
} | {name: _V7_ONLY_TERMS for name in _CURRENT_ONLY_DOCS if name != "AGENTS.md"}

# ADR-005 对 F1 锁定页的结论必须在四个真源一致，且不能留下相反口径。
_ADR_005_CONCLUSION_DOCS: Final[tuple[str, ...]] = (_SPEC, _WEB_SPEC, _ADR_018, _PLAN)
_ADR_005_CONCLUSION: Final = "不依赖 ADR-005"
_ADR_005_CONTRADICTION: Final = "未批准时 F1 不开放 /results"

# F1 契约字段名只有一个拼写。
_CONTRACT_NAME_DOCS: Final[tuple[str, ...]] = (_SPEC, _ADR_018, "ARCHITECTURE.md", _PLAN)
_RETIRED_CONTRACT_NAMES: Final[tuple[str, ...]] = ("submission_kind", "type_tag")
_COLUMN_SPEC: Final = "ColumnSpec(ordinal, name, type)"


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _named_docs(names: tuple[str, ...]) -> dict[str, str]:
    return {name: _read(name) for name in names}


def _all_docs() -> dict[str, str]:
    return _named_docs(tuple(set(_F1_TRUTH_TERMS) | set(_STALE_F1_PHRASES)))


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


def _adr_005_conflicts(docs: dict[str, str]) -> list[str]:
    return [
        name
        for name in _ADR_005_CONCLUSION_DOCS
        if _ADR_005_CONCLUSION not in docs[name] or _ADR_005_CONTRADICTION in docs[name]
    ]


def _contract_name_drift(docs: dict[str, str]) -> list[tuple[str, str]]:
    drift = [
        (name, retired)
        for name in _CONTRACT_NAME_DOCS
        for retired in _RETIRED_CONTRACT_NAMES
        if retired in docs[name]
    ]
    drift += [
        (name, _COLUMN_SPEC) for name in _CONTRACT_NAME_DOCS if _COLUMN_SPEC not in docs[name]
    ]
    return drift


def test_every_f1_truth_source_carries_its_decisions() -> None:
    assert _missing_terms(_all_docs()) == []


def test_no_truth_source_revives_retired_wording_or_v7_machinery() -> None:
    assert _stale_phrases(_all_docs()) == []


@pytest.mark.parametrize(
    "path",
    [_ADR_007, _ADR_009, _ADR_010, _ADR_012, _ADR_013, _ADR_017, _ADR_018],
)
def test_f1_adr_changes_stay_proposed_until_owner_acceptance(path: str) -> None:
    # 负责人接受前，F1 修订只能是 Proposed，不能把文档改动写成已接受。
    text = _read(path)
    assert "Proposed" in text
    assert "接受前不得写 F1 行为源码" in text


def test_adr_005_conclusion_for_the_locked_page_is_single_and_consistent() -> None:
    assert _adr_005_conflicts(_named_docs(_ADR_005_CONCLUSION_DOCS)) == []


def test_f1_contract_names_have_one_spelling() -> None:
    assert _contract_name_drift(_named_docs(_CONTRACT_NAME_DOCS)) == []
    assert 'discriminator="input_kind"' in _read(_PLAN)


def test_submission_is_one_transaction_with_one_entry_point() -> None:
    adr = _read(_ADR_018)
    plan = _read(_PLAN)
    assert "同一个 PostgreSQL 事务" in adr
    assert "downgrade：存在任一 `sql_artifact` 行时拒绝执行" in adr
    assert "逐字节不变" in adr
    assert "**唯一提交入口**" in plan
    assert "不新增公开的 Store 提交方法" in plan


def test_submission_compatibility_uses_rename_and_migration_only() -> None:
    plan = _read(_PLAN)
    assert "**显式改名**为 `ConversationSubmission`" in plan
    assert "`union_tag_not_found`" in plan
    assert "load_contract(TaskSubmission, ...)" in plan
    assert "旧数据兼容**只**靠下述 migration 回填" in _read(_ADR_018)


def test_dispatch_order_is_unchanged_without_deferral() -> None:
    # v8 没有调度退让，因此不给 tasks 加 created_at，也不改 dispatch_sort_key。
    plan = _read(_PLAN)
    assert "dispatch 排序与 `dispatch_sort_key` 不变" in plan
    assert "dispatch_order_key" not in plan
    assert "dispatch 候选规则与 `dispatch_sort_key` 不变" in _read(_ADR_010)


# 模型边界只有一种说法；所有承载 F1 SQL 规则的真源都写同一句，且不留旧说法。
_MODEL_BOUNDARY: Final = (
    "纯 SQL 消息、SqlArtifact 和最终执行字节不进入模型；混合对话可以进入模型。"
    "模型候选不能直接执行，只有完整展示、用户确认并绑定 hash 后，才能生成新的 SqlArtifact。"
)
_MODEL_BOUNDARY_DOCS: Final[tuple[str, ...]] = (
    "AGENTS.md",
    "ARCHITECTURE.md",
    _ADR_007,
    _ADR_017,
    _ADR_018,
    _SPEC,
    _PLAN,
)
_RETIRED_MODEL_WORDING: Final[tuple[str, ...]] = (
    "SQL 原文不进入模型",
    "SQL 原文不进入任何模型",
    "SQL 原文也不进入模型",
    "SQL 原文永不进入模型",
    "执行 SQL 不来自模型原文",
    "优先扩展现有慢查询",
)
_RETIRED_MODEL_WORDING_DOCS: Final[tuple[str, ...]] = (
    *_MODEL_BOUNDARY_DOCS,
    "DEVELOPMENT_PLAN.md",
    "AGENT_HANDOFF.md",
    _ROADMAP,
)


def _model_boundary_drift(docs: dict[str, str]) -> list[tuple[str, str]]:
    drift = [
        (name, "missing") for name in _MODEL_BOUNDARY_DOCS if _MODEL_BOUNDARY not in docs[name]
    ]
    drift += [
        (name, retired)
        for name in _RETIRED_MODEL_WORDING_DOCS
        for retired in _RETIRED_MODEL_WORDING
        if retired in docs[name]
    ]
    return drift


def test_model_boundary_has_one_wording_across_truth_sources() -> None:
    assert _model_boundary_drift(_named_docs(_RETIRED_MODEL_WORDING_DOCS)) == []


def test_embedded_sql_is_advisory_only_for_every_route() -> None:
    # 含“慢SQL”的混合消息会被现有关键词规则路由到慢查询诊断，
    # 所以 advisory-only 必须覆盖规则与模型两条路由。
    for name in (_SPEC, _ADR_017, _PLAN):
        text = _read(name)
        assert "contains_embedded_sql" in text, name
        assert "advisory-only" in text, name
    spec = _read(_SPEC)
    assert "无论来自模型还是现有关键词规则" in spec
    assert "Gateway 调用为 0" in spec
    assert "`test_f1_embedded_sql_is_advisory_only` 必须转红" in _read(_PLAN)


def test_result_ref_exists_before_evidence() -> None:
    # 现有 Runner 先构造 Evidence 再提交，result_ref 必须由 Runner 先生成，Store 只落库。
    assert (
        "Runner 先生成 CSPRNG `result_ref`，把同一个值写入 Evidence 与 `StepCommitCommand`"
        in _read(_SPEC)
    )
    assert "Runner 在构造 Evidence 前生成它" in _read(_ADR_018)
    assert "在 `_build_evidence` 前用 `secrets.token_urlsafe(32)` 生成 `result_ref`" in _read(_PLAN)
    for name in (_SPEC, _ADR_018, _ADR_009, _PLAN):
        assert "在结果写入的同一事务里创建" not in _read(name), name
        assert "写结果行、生成 CSPRNG" not in _read(name), name


def test_default_pool_fits_default_worker_concurrency() -> None:
    concurrency, pool = 4, 9
    assert pool >= 2 * concurrency + 1
    for name in (_SPEC, _ADR_010, _PLAN):
        assert "由 5 调为 9" in _read(name) or "由 5 改为 9" in _read(name), name
    assert "默认 `Settings()` 构造成功且满足校验" in _read(_PLAN)


def test_sql_artifact_expires_even_if_its_task_never_ends() -> None:
    for name in (_SPEC, _ADR_018, _PLAN, "ARCHITECTURE.md"):
        text = _read(name)
        assert "created_at + 24h" in text, name
        assert "只延不缩" in text, name
        assert "`sql_artifact.expired`" in text, name
    for name in (_SPEC, _ADR_018, _PLAN, "ARCHITECTURE.md"):
        assert "最后一个引用它的任务终态后 24" not in _read(name), name


def test_f1_guards_are_discriminating() -> None:
    docs = _all_docs()
    without_rule = dict(docs)
    without_rule["AGENTS.md"] = docs["AGENTS.md"].replace(
        "用户直接 SQL 只能来自受保护 SQL artifact", ""
    )
    assert ("AGENTS.md", "用户直接 SQL 只能来自受保护 SQL artifact") in _missing_terms(without_rule)
    revived = dict(docs)
    revived[_ADR_009] = docs[_ADR_009] + "\nschedule_deferral\n"
    assert _stale_phrases(revived) == [(_ADR_009, "schedule_deferral")]

    conclusion_docs = _named_docs(_ADR_005_CONCLUSION_DOCS)
    reverted = dict(conclusion_docs)
    reverted[_SPEC] = conclusion_docs[_SPEC] + "\n" + _ADR_005_CONTRADICTION
    assert _adr_005_conflicts(reverted) == [_SPEC]

    names = _named_docs(_CONTRACT_NAME_DOCS)
    renamed = dict(names)
    renamed[_PLAN] = names[_PLAN] + "\nsubmission_kind"
    assert _contract_name_drift(renamed) == [(_PLAN, "submission_kind")]

    boundary = _named_docs(_RETIRED_MODEL_WORDING_DOCS)
    old_adr = dict(boundary)
    old_adr[_ADR_007] = boundary[_ADR_007].replace(_MODEL_BOUNDARY, "SQL 原文也不进入模型端口。")
    assert _model_boundary_drift(old_adr) == [
        (_ADR_007, "missing"),
        (_ADR_007, "SQL 原文也不进入模型"),
    ]


# 负责人 2026-09-27：当前范围只叫 F1-Core；完整能力须再有 F1-NL，结果说明只在报错时做。
_F1_CORE_DOCS: Final[tuple[str, ...]] = (
    _SPEC,
    _PLAN,
    "DEVELOPMENT_PLAN.md",
    _ROADMAP,
    "AGENT_HANDOFF.md",
)


def test_f1_core_is_not_reported_as_the_full_agent_query_capability() -> None:
    docs = _named_docs(_F1_CORE_DOCS)
    assert [name for name, text in docs.items() if "F1-Core" not in text] == []
    spec = docs[_SPEC]
    assert "### 12.1 完成定义" in spec
    assert "**F1-NL 完成**" in spec
    assert "**只在报错时做**" in spec
    assert "不需要模型、不需要 F2，也不修订 ADR-015" in spec

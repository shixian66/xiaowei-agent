"""同一事实在两份文档里各写一份时，必须有东西把它们钉住。

Codex 第二轮复审打回的第 2 项：验收报告写着「计划 V1.4」而计划头部已经是 V1.5。
这不是笔误，是**结构问题**——版本号在两个文件里各存一份，改一份不会让任何东西失败。
同一轮里还漂了「HEAD = 797a210」「T10 尚未落成提交」「PR 尚未开」三处，成因相同：
改了正在编辑的那一处，没有扫描同一事实的其余副本。

本文件只钉**能机械判定**的那一类。"某句散文是否已经过期"没法自动判，那类靠复审；
但"两个文件里的同一个版本号"可以判，而它恰好是这次漏掉的那一个。
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PLAN = _ROOT / "docs/plans/M4-postgres-taskstore.md"
_REPORT = _ROOT / "docs/handoff/M4-acceptance-report.md"

_PLAN_TEXT = _PLAN.read_text(encoding="utf-8")
_REPORT_TEXT = _REPORT.read_text(encoding="utf-8")

_VERSION = re.compile(r"V(\d+\.\d+)")


def plan_version() -> str:
    """计划的权威版本号：标题里那个。"""
    title = _PLAN_TEXT.splitlines()[0]
    match = _VERSION.search(title)
    assert match, f"计划标题里没有版本号：{title}"
    return match.group(1)


def report_cited_plan_version() -> str:
    """验收报告「受审对象」表里引用的计划版本。"""
    match = re.search(r"M4-postgres-taskstore\.md\)\s*V(\d+\.\d+)", _REPORT_TEXT)
    assert match, "验收报告没有引用计划版本"
    return match.group(1)


def test_the_report_cites_the_current_plan_version() -> None:
    assert report_cited_plan_version() == plan_version(), (
        f"报告引用 V{report_cited_plan_version()}，计划已是 V{plan_version()}："
        "同一事实的两份拷贝漂了"
    )


def test_the_plan_changelog_mentions_its_own_current_version() -> None:
    """反空洞：版本号只改标题、不在头部变更记录里说明改了什么，等于没有版本。

    这条同时防住"为了让上一条通过而把报告里的数字一改了事"——真正改了版本就必须
    在计划里留下一句它改了什么。
    """
    current = plan_version()
    header = "\n".join(_PLAN_TEXT.splitlines()[:10])
    assert f"V{current}" in header, f"计划头部没有 V{current} 的变更说明"


def test_the_binding_is_discriminating() -> None:
    """反例：确认这两个抽取函数真的各自读到了东西，而不是恰好都返回同一个常量。"""
    assert _VERSION.search("V9.9").group(1) == "9.9"
    probe = re.search(
        r"M4-postgres-taskstore\.md\)\s*V(\d+\.\d+)", "M4-postgres-taskstore.md) V9.9"
    )
    assert probe is not None and probe.group(1) == "9.9"
    assert plan_version() != "9.9"


# --- RI5 成套接受门 -----------------------------------------------------------
#
# 同一个根因的第二个实例。RI5 的"哪几份文档必须一并重新接受"这句话，被复述在
# ADR-007、ADR-014、ADR-015、总体 spec、RI5 设计（两处）、DEVELOPMENT_PLAN 和
# ARCHITECTURE 里。集合前后扩大过两次（先补 ADR-007，再补 spec），每次都只改了
# 当时想起来的那几处——复审两轮分别抓到漏网的 2 处和 4 处。
#
# 措辞是散文，判不了；但"这句话在每份枚举它的文档里逐字一致"可以判，而这正是
# 两次漂移的形状。

_RI5_ACCEPTANCE_SET = (
    "ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订已由项目负责人于 2026-09-14 "
    "成套接受；该接受不授予 RI3 PR 3E 与 RI2 的真实调用 GO。"
)

# 每一份都在正文里独立复述过这个集合，因此每一份都必须逐字带上它。
_RI5_GATE_DOCS = (
    "ARCHITECTURE.md",
    "DEVELOPMENT_PLAN.md",
    "docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md",
    "docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md",
    "docs/adr/ADR-015-real-model-provider-boundary.md",
    "docs/plans/RI5-local-web-admin-simplified-design.md",
    "docs/superpowers/specs/2026-09-10-real-integrations-design.md",
)

# 集合曾经写成这些形状；任何一处复活都说明又漏改了一份。
_STALE_RI5_GATE_PHRASES = (
    "ADR-014、ADR-015 修订并重新接受",
    "ADR-014、ADR-015 已按 RI5 设计修订并重新接受",
    "三份 ADR 必须",
    "三份修订均为 Proposed",
    "ADR-007/014/015 的 RI5 修订与重新接受",
    # 2026-09-14 项目负责人成套接受后，"待接受"措辞整体过期。
    "必须成套复核并重新接受",
    "任一份未被接受，RI5 都不得开工",
)


def test_every_ri5_gate_doc_states_the_same_acceptance_set() -> None:
    missing = [
        name
        for name in _RI5_GATE_DOCS
        if _RI5_ACCEPTANCE_SET not in (_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not missing, (
        f"这些文档复述了 RI5 接受门却与 canonical 措辞不一致：{missing}。"
        "集合变化时必须同时改全部枚举点，不能只改正在编辑的那一份"
    )


def test_no_document_still_carries_a_superseded_acceptance_set() -> None:
    """正向断言挡不住"新句子加上了、旧句子忘了删"，所以再钉一次旧形状。"""
    found = [
        (name, phrase)
        for name in _RI5_GATE_DOCS
        for phrase in _STALE_RI5_GATE_PHRASES
        if phrase in (_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not found, f"仍在使用被取代的 RI5 接受集合措辞：{found}"


def test_the_ri5_gate_binding_is_discriminating() -> None:
    """反例：确认上面两条不是空洞通过。

    canonical 句必须真的能在文本里被判定，且旧形状确实与它不同——否则
    "全都包含"和"全都不包含"可以同时因为写错常量而恒真。
    """
    assert _RI5_ACCEPTANCE_SET not in "任意无关文本"
    assert _RI5_GATE_DOCS, "枚举点集合为空，两条断言会恒真"
    for phrase in _STALE_RI5_GATE_PHRASES:
        assert phrase not in _RI5_ACCEPTANCE_SET, (
            f"旧形状 {phrase!r} 是 canonical 句的子串，禁用断言会恒假"
        )


# --- I0/I1 智能交互入口文档门 -------------------------------------------------

_ADR_017 = _ROOT / "docs/adr/ADR-017-intelligent-interaction-and-clarification.md"
_ADR_009 = _ROOT / "docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md"
_ADR_017_REQUIRED_TERMS = (
    "InteractionKind",
    "RoutingDisposition",
    "CLARIFICATION_REQUIRED",
    "clarification_parent_task_id",
    "ClarificationRecordStore",
    "CapabilityInputBinding",
    "ReadClass",
    "ExecutionDisclosure",
)
_ADR_009_V2_TERMS = (
    "I1-C 修订",
    "`read_class`",
    "`PLAN_SCHEMA_VERSION` 当前取值为 `2`",
    "旧 V1 plan 读取后 fail-closed",
)


def test_adr_017_freezes_the_i0_i1_interaction_terms() -> None:
    assert _ADR_017.exists(), "ADR-017 缺失，I0/I1 设计不能只停留在 superpowers 计划稿"
    text = _ADR_017.read_text(encoding="utf-8")
    missing = [term for term in _ADR_017_REQUIRED_TERMS if term not in text]
    assert not missing, f"ADR-017 未冻结这些 I0/I1 术语：{missing}"


def test_adr_009_records_i1c_plan_schema_v2_terms() -> None:
    assert _ADR_009.exists(), "ADR-009 缺失，plan_hash 规范真源不可缺"
    text = _ADR_009.read_text(encoding="utf-8")
    missing = [term for term in _ADR_009_V2_TERMS if term not in text]
    assert not missing, f"ADR-009 未同步 I1-C Plan schema V2 事实：{missing}"


_I0_TRUTH_DOC_TERMS = {
    "ARCHITECTURE.md": (
        "→ load-or-create AcceptedInteractionArtifact",
        "→ DeterministicInteractionRouter",
        "PipelineStage.DISCLOSURE",
        "十一个阶段",
    ),
    "DEVELOPMENT_PLAN.md": (
        "I0 文档与契约",
        "I1 安全分流/澄清/披露",
        "InteractionArtifact → DeterministicInteractionRouter → CapabilityResolver → "
        "SlotVerifier → PlanCompiler",
    ),
    "README.md": (
        "智能交互入口 I0-DOC 已绑定",
        "I1-A–I1-D 与 I2 均已合入 `main`，I2 已归档",
        "`knowledge_lookup` 与 `log_analysis` 仍保持拒绝",
        "InteractionArtifact → Router → Resolver → SlotVerifier → PlanCompiler",
    ),
    "AGENT_HANDOFF.md": (
        "I0-DOC 目标",
        "`main` 已包含 I1-A Task 1.1–1.3",
        "Runtime 在 Resolver 前读写",
        "ADR-017",
    ),
}

_I0_TRACE_DOC_TERMS = {
    "docs/adr/ADR-017-intelligent-interaction-and-clarification.md": (
        "PipelineStage.DISCLOSURE",
        "十一个阶段",
        "ModelCallKind.INTERACTION",
    ),
    "ARCHITECTURE.md": (
        "PipelineStage.DISCLOSURE",
        "十一个阶段",
        "ModelCallKind.INTERACTION",
    ),
}


def _missing_i0_truth_terms(docs: dict[str, str]) -> dict[str, list[str]]:
    missing = {
        name: [term for term in terms if term not in docs[name]]
        for name, terms in _I0_TRUTH_DOC_TERMS.items()
    }
    return {name: terms for name, terms in missing.items() if terms}


def test_i0_truth_docs_are_bound_to_adr_017() -> None:
    docs = {name: (_ROOT / name).read_text(encoding="utf-8") for name in _I0_TRUTH_DOC_TERMS}
    missing = _missing_i0_truth_terms(docs)
    assert not missing, f"I0 真相文档未同步 ADR-017 入口口径：{missing}"


def test_i0_trace_docs_are_bound_to_disclosure_stage() -> None:
    missing = {
        name: [term for term in terms if term not in (_ROOT / name).read_text(encoding="utf-8")]
        for name, terms in _I0_TRACE_DOC_TERMS.items()
    }
    missing = {name: terms for name, terms in missing.items() if terms}
    assert not missing, f"I0 trace/错误归因文档未同步披露阶段口径：{missing}"


def _replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    assert count == 1, f"测试反例锚点不唯一或不存在：{old!r}"
    return text.replace(old, new, 1)


def test_i0_truth_doc_binding_is_discriminating() -> None:
    """反例：删除 I0 承重段落时，绑定测试必须能红。"""
    docs = {name: (_ROOT / name).read_text(encoding="utf-8") for name in _I0_TRUTH_DOC_TERMS}

    readme_i0_status = (
        "> 智能交互入口 I0-DOC 已绑定 "
        "[ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md)。\n"
        "> I1-A–I1-D 与 I2 均已合入 `main`，I2 已归档。普通对话的回答是"
        "**由当前 `CapabilitySnapshot`\n"
        "> 确定性投影出来的能力目录**：逐条列出已注册能力、操作、`read_class` 与所经 "
        "gateway，并带上\n"
        "> 快照标识作为来源；回答只由声明决定，与用户文本无关，不调用工具、"
        "不访问外部系统、不读取历史。\n"
        "> `knowledge_lookup` 与 `log_analysis` 仍保持拒绝并分别留给 I3/I4；I3 尚未开始。\n"
        "> 能力目录只是把 Registry 声明重排给用户看，**不表示这些能力已经连接真实系统**"
        "——不能把 I2\n"
        "> 写成真实模型、资料查询、日志分析、真实渠道、真实目标、部署或用户验收。\n"
    )
    without_readme_status = {
        **docs,
        "README.md": _replace_once(docs["README.md"], readme_i0_status, ""),
    }
    readme_missing = _missing_i0_truth_terms(without_readme_status)
    assert "README.md" in readme_missing
    assert "I1-A–I1-D 与 I2 均已合入 `main`，I2 已归档" in readme_missing["README.md"]

    arch_i0_chain = (
        "            → load-or-create AcceptedInteractionArtifact\n"
        "            → DeterministicInteractionRouter\n"
    )
    without_arch_chain = {
        **docs,
        "ARCHITECTURE.md": _replace_once(docs["ARCHITECTURE.md"], arch_i0_chain, ""),
    }
    arch_missing = _missing_i0_truth_terms(without_arch_chain)
    assert "ARCHITECTURE.md" in arch_missing
    assert "→ load-or-create AcceptedInteractionArtifact" in arch_missing[
        "ARCHITECTURE.md"
    ]


def test_i0_truth_docs_do_not_revive_stale_entry_shapes() -> None:
    stale_phrases = (
        "Context → IntentDraft → Resolver → PlanCompiler",
        "ContextAssembler（只有显式 parent 时）",
    )
    found = [
        (name, phrase)
        for name in _I0_TRUTH_DOC_TERMS
        for phrase in stale_phrases
        if phrase in (_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not found, f"I0 真相文档仍在使用旧入口/父上下文形状：{found}"


_I1D_TRUTH_DOC_TERMS = {
    "README.md": (
        "I1-A–I1-D 与 I2 均已合入 `main`，I2 已归档",
        "不能把 I2\n> 写成真实模型",
        # 能力目录是**投影**出来的，不是模型答的；README 必须说清这一点，
        # 否则"小维会介绍自己的能力"很容易被读成模型自由问答已经开放。
        "由当前 `CapabilitySnapshot` 确定性投影出来的能力目录",
    ),
    "AGENT_HANDOFF.md": (
        "I1-D Eval/closure",
        "`SlotVerifier`/可信槽位升级已完成离线实现",
        "ReadClass/Plan schema V2 已完成离线实现",
        "ExecutionDisclosure 执行披露屏障已完成离线实现",
        "真实 PostgreSQL 证据仅来自下条记录的隔离临时容器",
    ),
}

_STALE_I1D_PHRASES = (
    "可信槽位、`ReadClass` 和执行披露屏障仍未实现",
    "尚未实现 `SlotVerifier`",
    "`SlotVerifier`、ReadClass/Plan schema V2、ExecutionDisclosure",
    "`ReadClass` 和执行披露屏障仍未实现",
    "ReadClass/Plan schema V2 与 ExecutionDisclosure 仍未实现",
    "ExecutionDisclosure 仍未实现",
    "执行披露屏障仍未实现",
)


def test_i1d_truth_docs_reflect_execution_disclosure_scope() -> None:
    missing = {
        name: [
            term
            for term in terms
            if term not in (_ROOT / name).read_text(encoding="utf-8")
        ]
        for name, terms in _I1D_TRUTH_DOC_TERMS.items()
    }
    missing = {name: terms for name, terms in missing.items() if terms}
    assert not missing, f"I1-D 真相文档未同步 ExecutionDisclosure 当前范围：{missing}"


def test_i1d_truth_docs_do_not_revive_stale_i1c_scope() -> None:
    found = [
        (name, phrase)
        for name in _I1D_TRUTH_DOC_TERMS
        for phrase in _STALE_I1D_PHRASES
        if phrase in (_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not found, f"I1-D 真相文档仍含旧范围口径：{found}"


_I2_ARCHIVE = "docs/handoff/archive/2026-09-20-I2-bounded-conversation.md"

_I2B_TRUTH_DOC_TERMS = {
    # README 的"目标能力"段和顶部状态块是**两处独立叙述**，只钉住顶部时下面那段可以
    # 长期停在旧口径上——I2-B 就是这么漏的。两段各自钉一条。
    "README.md": (
        "由当前 `CapabilitySnapshot` 确定性投影出来的能力目录",
        "I2 的普通对话只返回由当前 `CapabilitySnapshot` 确定性投影出来的能力目录",
    ),
    "ARCHITECTURE.md": (
        "conversation: deterministic capability-catalog response",
        "回答与用户文本无关",
    ),
    "docs/adr/ADR-017-intelligent-interaction-and-clarification.md": (
        "回答只由快照决定，不接受用户文本",
        "投影的是当前快照，不是任务创建时的快照",
        "空快照必须明说",
    ),
    # 归档后 I2 的细节真相面在归档文里，AGENT_HANDOFF 只负责指过去并声明范围。
    # 绑定跟着真相走：把细节条目留在 handoff 上，会逼着以后每一轮都把已归档的内容
    # 重新抄一遍，而那正是这份文件开头明说不做的事。
    "AGENT_HANDOFF.md": (
        "I2 限定领域普通对话已完成离线实施并归档",
        _I2_ARCHIVE,
    ),
    _I2_ARCHIVE: (
        "`RoutingDisposition` 增加 `respond`",
        "回答是当前 `CapabilitySnapshot` 的确定性投影",
        "每条终态任务恰好一条 `REFLECTION`",
        "把用户文本回显进对话回答（模拟注入面）",
        "当前不可达",
        "没有部署、canary 或产品",
        "`tests`",
    ),
}

_STALE_I2_CONVERSATION_PHRASES = (
    "I2-A 的普通对话只返回限定领域固定回复",
    "只返回固定、限定领域的确定性回复",
    "TaskView 从已落库\n  `TaskSubmission` 重建固定回复",
    "普通对话只返回固定回复",
    # `respond` 的定义句本身也曾停在"固定回复"上；枚举定义是最容易被当成
    # "只是术语说明"而漏掉的一处，所以单列。
    "仅用于限定领域普通对话的\n无工具固定回复",
)


def test_i2b_truth_docs_describe_the_capability_catalog_answer() -> None:
    missing = {
        name: [
            term
            for term in terms
            if term not in (_ROOT / name).read_text(encoding="utf-8")
        ]
        for name, terms in _I2B_TRUTH_DOC_TERMS.items()
    }
    missing = {name: terms for name, terms in missing.items() if terms}
    assert not missing, f"I2-B 真相文档未同步能力目录口径：{missing}"


def test_i2b_truth_docs_do_not_revive_the_fixed_answer_scope() -> None:
    """旧口径不能回潮。

    "固定回复"和"能力目录"是两个**不同的产品承诺**：前者读起来像"小维只会说一句
    客套话"，后者是"小维会把自己的声明摊开给你看"。文档停在前者，读者就会低估已经
    交付的东西，也看不出这条通道现在有来源引用。
    """
    found = [
        (name, phrase)
        for name in _I2B_TRUTH_DOC_TERMS
        for phrase in _STALE_I2_CONVERSATION_PHRASES
        if phrase in (_ROOT / name).read_text(encoding="utf-8")
    ]
    assert not found, f"真相文档仍含 I2-A 固定回复口径：{found}"


def test_i2_archive_does_not_overstate_the_evidence_level() -> None:
    """归档文最容易被当成"这件事已经上线了"来读。

    它记录的是**离线实施范围**验收：证据等级仍是 `tests`，三条能力都没有连接真实系统。
    归档一旦写成肯定式的部署/验收声称，后面每一个读它的人都会据此高估当前能力。
    """
    text = (_ROOT / _I2_ARCHIVE).read_text(encoding="utf-8")
    for claim in (
        "已部署",
        "已上线",
        "已 canary",
        "已用户验收",
        "已连接真实",
        "deployed SHA",
    ):
        assert claim not in text
    assert "没有部署、canary 或产品" in text
    assert "只关闭 I2 的**离线实施范围**" in text

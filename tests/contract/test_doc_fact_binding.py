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
from typing import Final

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PLAN = _ROOT / "docs/plans/M4-postgres-taskstore.md"
_REPORT = _ROOT / "docs/handoff/M4-acceptance-report.md"

_PLAN_TEXT = _PLAN.read_text(encoding="utf-8")
_REPORT_TEXT = _REPORT.read_text(encoding="utf-8")

_VERSION = re.compile(r"V(\d+\.\d+)")


def _check_current_status_reference(source: str, handoff: str) -> None:
    """当前状态入口必须指向唯一真源及真实存在的锚点。"""
    targets = re.findall(r"\[当前状态\]\(([^)]+)\)", source)
    assert targets == ["AGENT_HANDOFF.md#current-status"], targets
    assert handoff.count('<a id="current-status"></a>') == 1
    # 状态链接之外的阶段流水会再次产生可独立漂移的副本。
    for pattern in (
        r"main@[0-9a-f]+",
        r"\d+ passed",
        r"已离线实现",
        r"当前开发机",
        r"当前状态[：:]\s*(?:W\d|RI\d|M\d|I\d)",
    ):
        assert not re.search(pattern, source), pattern


@pytest.mark.parametrize("name", ["README.md", "ARCHITECTURE.md"])
def test_current_status_entry_points_to_the_single_handoff(name: str) -> None:
    source = (_ROOT / name).read_text(encoding="utf-8")
    _check_current_status_reference(source, (_ROOT / "AGENT_HANDOFF.md").read_text())


@pytest.mark.parametrize(
    ("source", "handoff", "valid"),
    [
        ('[当前状态](AGENT_HANDOFF.md#current-status)', '<a id="current-status"></a>', True),
        ('[当前状态](README.md#current-status)', '<a id="current-status"></a>', False),
        ('[当前状态](AGENT_HANDOFF.md#missing)', '<a id="current-status"></a>', False),
        ('[当前状态](AGENT_HANDOFF.md#current-status)', '', False),
        ('无链接', '<a id="current-status"></a>', False),
        (
            '[当前状态](AGENT_HANDOFF.md#current-status)\nW1b 已离线实现',
            '<a id="current-status"></a>',
            False,
        ),
        (
            '[当前状态](AGENT_HANDOFF.md#current-status)\n## 启动\n说明\n'
            '当前状态：W1a 详细实施计划送审，计划获批后才可开始',
            '<a id="current-status"></a>',
            False,
        ),
        (
            '[当前状态](AGENT_HANDOFF.md#current-status)\n## 验证\n'
            '当前开发机缺少 Docker，本轮没有运行验证',
            '<a id="current-status"></a>',
            False,
        ),
    ],
)
def test_current_status_reference_rejects_broken_or_duplicated_truth(
    source: str, handoff: str, valid: bool
) -> None:
    if valid:
        _check_current_status_reference(source, handoff)
    else:
        with pytest.raises(AssertionError):
            _check_current_status_reference(source, handoff)


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
        "[当前状态](AGENT_HANDOFF.md#current-status)",
        "InteractionArtifact → Router → Resolver → SlotVerifier → PlanCompiler",
    ),
    "AGENT_HANDOFF.md": (
        "I1-D Eval/closure",
        "Task 1.4/1.5",
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

    without_readme_link = {
        **docs,
        "README.md": _replace_once(
            docs["README.md"], "[当前状态](AGENT_HANDOFF.md#current-status)", ""
        ),
    }
    assert "README.md" in _missing_i0_truth_terms(without_readme_link)

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


_STALE_I0_ENTRY_SHAPES = (
    "Context → IntentDraft → Resolver → PlanCompiler",
    "ContextAssembler（只有显式 parent 时）",
    "RI3 的已保存 advisory 可作为后续显式父链的模型输入",
    "显式历史会把既存文本再次发送给 provider",
    "The 64,000-character history limit",
    "RI3 的 Web parent 只增加",
)


def _stale_i0_entry_shapes(docs: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (name, phrase)
        for name, text in docs.items()
        for phrase in _STALE_I0_ENTRY_SHAPES
        if phrase in text
    ]


def test_i0_truth_docs_do_not_revive_stale_entry_shapes() -> None:
    docs = {name: (_ROOT / name).read_text(encoding="utf-8") for name in _I0_TRUTH_DOC_TERMS}
    found = _stale_i0_entry_shapes(docs)
    assert not found, f"I0 真相文档仍在使用旧入口/父上下文形状：{found}"


@pytest.mark.parametrize("stale", _STALE_I0_ENTRY_SHAPES)
def test_i0_entry_guard_rejects_restored_legacy_parent_requirements(stale: str) -> None:
    current = "分类请求仅含本轮文本与澄清，真实调用仍须独立 GO。"
    assert not _stale_i0_entry_shapes({"AGENT_HANDOFF.md": current})
    restored = f"{current}\n## 当前验收要求\n{stale}"
    assert _stale_i0_entry_shapes({"AGENT_HANDOFF.md": restored}) == [
        ("AGENT_HANDOFF.md", stale)
    ]


_I1D_TRUTH_DOC_TERMS = {
    "AGENT_HANDOFF.md": (
        "I1-D Eval/closure",
        "`SlotVerifier`/可信槽位升级已完成离线实现",
        "ReadClass/Plan schema V2 已完成离线实现",
        "ExecutionDisclosure 执行披露屏障已完成离线实现",
        "`tests`",
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
    # README 仅保留能力行为说明；实施进度在 handoff / I2 归档。
    "README.md": (
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


# --- Web 运维工作台产品规格门 --------------------------------------------
#
# V0.2 在正文里改了 RI5 的读权限、首次改密和 OAuth state 形状，
# 却没有把它们登记到「对既有规范的影响」；同时又把激活审批放在
# AdminAuditStore 之前。两者都不是措辞问题：前者会让 W0 漏改承重 ADR，
# 后者会让「审计不可写时 fail-closed」在 W1b 无实现载体。这里只钉这两个
# 可机械判定的集合/顺序，不为人类散文做逐句断言。

_WEB_PRODUCT_SPEC = (
    _ROOT
    / "docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md"
)
_WEB_PRODUCT_ADR_CHANGES = frozenset(
    {
        "ADR-007 D5",
        "ADR-014 RI5 R1",
        "ADR-014 RI5 R2",
        "ADR-014 RI5 R3",
    }
)


def _section_between(text: str, *, start: str, end: str) -> str:
    start_at = text.find(start)
    assert start_at >= 0, f"规格缺少承重段落：{start}"
    end_at = text.find(end, start_at + len(start))
    assert end_at >= 0, f"规格缺少段落边界：{end}"
    return text[start_at:end_at]


def _accepted_adr_change_clauses(text: str) -> frozenset[str]:
    ledger = _section_between(
        text,
        start="### 19.2 本规格取消或收窄的既有条款",
        end="### 19.3 W0 同步文档",
    )
    return frozenset(
        re.findall(r"^\| `(ADR-\d{3} [^`]+)` \|", ledger, re.M)
    )


def _delivery_stage(text: str, *, name: str, next_name: str) -> str:
    delivery = _section_between(
        text,
        start="### 17.1 当前可交付序列",
        end="### 17.2 独立阻塞门",
    )
    return _section_between(
        delivery,
        start=f"**{name} ",
        end=f"**{next_name} ",
    )


def test_web_product_spec_lists_every_accepted_adr_change() -> None:
    text = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    assert _accepted_adr_change_clauses(text) == _WEB_PRODUCT_ADR_CHANGES, (
        "Web 产品规格正文已改变的 ADR 条款与 W0 修订闭集不一致"
    )


def test_web_product_spec_builds_audit_writes_before_activation_approval() -> None:
    text = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    w1a = _delivery_stage(text, name="W1a", next_name="W1b")
    w1b = _delivery_stage(text, name="W1b", next_name="W2")
    assert "AdminAuditStore" in w1a and "append-only 写入契约" in w1a
    assert "CAS 审批" in w1b


def test_web_product_spec_gates_are_discriminating() -> None:
    """反例：漏掉一条 ADR 或抽走 W1a 审计底座时，上面的门必须真能变红。"""
    text = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    r2_row = next(
        line for line in text.splitlines() if line.startswith("| `ADR-014 RI5 R2`")
    )
    without_r2 = _replace_once(text, f"{r2_row}\n", "")
    assert _accepted_adr_change_clauses(without_r2) == (
        _WEB_PRODUCT_ADR_CHANGES - {"ADR-014 RI5 R2"}
    )
    with_unreviewed_clause = _replace_once(
        text,
        "### 19.3 W0 同步文档",
        "| `ADR-999 D1` | old | new | action |\n\n### 19.3 W0 同步文档",
    )
    assert _accepted_adr_change_clauses(with_unreviewed_clause) == (
        _WEB_PRODUCT_ADR_CHANGES | {"ADR-999 D1"}
    )

    without_audit_write = _replace_once(
        text,
        "`AdminAuditStore` 持久化与\n   append-only 写入契约",
        "审计写入尚未交付",
    )
    w1a = _delivery_stage(without_audit_write, name="W1a", next_name="W1b")
    assert not ("AdminAuditStore" in w1a and "append-only 写入契约" in w1a)


# --- W0 规格收口门 --------------------------------------------------------
#
# 上面的闭集只保证「四条 ADR 的名字还在」。W0 真正的风险是另一种：
# 名字一个不少，却把某一行的旧口径、新口径或必做动作悄悄改宽，于是
# W0 照着一份已被改写的授权去修 ADR。因此这里按**单元格**钉死四行，
# 并把「谁批准的、批准到哪一步」一起变成可机械判定的事实。

_W0_OWNER_APPROVAL = (
    "https://github.com/shixian66/xiaowei-agent/pull/59#issuecomment-5750387268"
)
_WEB_PRODUCT_ADR_CHANGE_ROWS: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "ADR-014 RI5 R1",
        "配置读取/保存/测试只接受 `LOCAL_ADMIN`，飞书 principal 不得读配置状态",
        "只为飞书认证 `ADMIN` 增加第 6.1 节的脱敏状态投影；原始配置读取、保存和测试仍禁止",
        "修订 R1，冻结状态 DTO 闭集和反例",
    ),
    (
        "ADR-014 RI5 R2",
        "LAN override 只能在 loopback 首次强制改密完成后启用",
        "项目负责人批准受信 LAN 在改密前可达；用“部署文档醒目警示 + 边缘限流 + 立即改密 + "
        "改密前路由闭集”替代 loopback 先后硬门",
        "在 R2 单独记录负责人批准、风险、补偿措施与回滚方式",
    ),
    (
        "ADR-014 RI5 R3",
        "本轮 schema 解冻不包含 `web_oauth_states`；新增 state 列或表必须先修 ADR",
        "允许且仅允许新增登录域 `web_oauth_login_contexts`，用 state digest 做唯一 PK/FK；"
        "连接测试 state 不产生 context",
        "在 R3 精确放开该表、事务/清理不变量和禁止任意 URL 字段",
    ),
    (
        "ADR-007 D5",
        "任何非 `LOCAL_ADMIN` 读写配置或发起探针都命中变更门",
        "只放开飞书 `ADMIN` 读脱敏状态投影；配置读 DTO、写入、Secret 和 probe 不放开",
        "修订 D5 的“读”半句并保留其余现场 GO/证据门",
    ),
)


def _accepted_adr_change_rows(text: str) -> tuple[tuple[str, ...], ...]:
    """按文档顺序返回 §19.2 四行的完整四元组（条款、旧口径、新口径、必做动作）。"""
    ledger = _section_between(
        text,
        start="### 19.2 本规格取消或收窄的既有条款",
        end="### 19.3 W0 同步文档",
    )
    rows: list[tuple[str, ...]] = []
    for line in ledger.splitlines():
        if not line.startswith("| `ADR-"):
            continue
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        rows.append((cells[0].strip("`"), *cells[1:]))
    return tuple(rows)


def _spec_header(text: str) -> str:
    return _section_between(text, start="# 小维 Web 运维工作台", end="## 1. 大白话结论")


def test_web_product_spec_records_approval_without_claiming_implementation() -> None:
    text = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    header = _spec_header(text)
    assert "Approved V0.4" in header
    assert "Review Draft" not in header
    assert "只授权编写 W4a/W4b 总实施计划" in header
    # V0.3 的负责人永久链接必须保留；V0.4 范围决定由本次计划 PR 持久化，不能拿旧链接冒充。
    assert _W0_OWNER_APPROVAL in header
    assert "V0.4 决策来源" in header and "本次计划 PR" in header
    assert "4e5a844620b700e25d6a29e43687c1a4c876db16" in header
    # 批准状态不等于实现状态：证据等级一栏必须继续否认这五类证据。
    assert "没有本修订对应的源码、运行、部署、真实外部调用或用户验收证据" in header
    assert _accepted_adr_change_rows(text) == _WEB_PRODUCT_ADR_CHANGE_ROWS, (
        "§19.2 四行内容已偏离负责人批准的授权范围"
    )


def test_web_product_spec_keeps_w4_source_and_live_use_outside_plan_authority() -> None:
    text = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    header = _spec_header(text)
    assert "计划获批后才可" in header
    for overclaim in (
        "W4a 已开始",
        "W4b 已开始",
        "已实现",
        "已部署",
        "已 canary",
        "已用户验收",
    ):
        assert overclaim not in header


def test_web_product_spec_approval_gates_are_discriminating() -> None:
    """反例：改任一单元格、撤掉批准来源或把状态写回草稿，门必须真能变红。"""
    text = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")

    widened = _replace_once(
        text,
        "只放开飞书 `ADMIN` 读脱敏状态投影；配置读 DTO、写入、Secret 和 probe 不放开",
        "放开飞书 `ADMIN` 读写配置与 probe",
    )
    assert _accepted_adr_change_rows(widened) != _WEB_PRODUCT_ADR_CHANGE_ROWS

    reordered = _replace_once(text, "| `ADR-007 D5` |", "| `ADR-007 D5 ` |")
    assert _accepted_adr_change_rows(reordered) != _WEB_PRODUCT_ADR_CHANGE_ROWS

    without_source = _replace_once(text, _W0_OWNER_APPROVAL, "https://example.invalid/pr/59")
    assert _W0_OWNER_APPROVAL not in _spec_header(without_source)

    back_to_draft = _replace_once(text, "Approved V0.4", "Review Draft V0.4")
    assert "Approved V0.4" not in _spec_header(back_to_draft)


# --- W0 ADR 修订门 --------------------------------------------------------
#
# 这一组的根因和上面一组是同一个，但换了个藏身处：amendment 里写清楚
# 「旧规则已被取代」，而 ADR 另一端的变更门仍以**未加限定的现行规则**
# 出现。抽取窗口内外各有一套答案，两套都能被引用。所以除了 amendment
# 本身，还要单独抽取窗口之外的变更门，逐条验证它指向同一个替代关系。

_ADR_DIR = _ROOT / "docs/adr"
_ADR_007 = (
    _ADR_DIR
    / "ADR-007-first-capabilities-execution-context-and-live-call-authorization.md"
)
_ADR_013 = _ADR_DIR / "ADR-013-m7-channel-boundary.md"
_ADR_014 = _ADR_DIR / "ADR-014-real-feishu-oauth-and-web-activation.md"
_ADR_015 = _ADR_DIR / "ADR-015-real-model-provider-boundary.md"

_W0_AMENDMENT_HEADING = "## Web 产品修订（2026-09-20）"
_W0_RETURN_INTENTS: Final[tuple[str, ...]] = (
    "WORKBENCH",
    "SAFE_TASK_DETAIL(task_id)",
    "ADMIN_CENTER",
    "ACTIVATION_STATUS(request_id)",
)
_CHANGE_GATE_BOUNDS: Final[dict[str, tuple[str, str | None]]] = {
    _ADR_007.name: ("### D5 变更门", "### D6 写权限的开放条件"),
    _ADR_013.name: ("## 回滚与变更门", None),
    _ADR_014.name: ("## 回滚与变更门", None),
    _ADR_015.name: ("## 变更门", "## 参考资料"),
}


def _web_product_amendment(path: Path) -> str:
    """只取统一 Web 产品修订段，避免把历史 RI5 原文误当现行增补。"""
    return _section_between(
        path.read_text(encoding="utf-8"),
        start=_W0_AMENDMENT_HEADING,
        end="## 后果",
    )


def _amendment_metadata(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in _web_product_amendment(path).splitlines():
        matched = re.match(r"^- (状态|决策人|决策日期|批准出处): (.+)$", line.strip())
        if matched:
            fields[matched.group(1)] = matched.group(2).strip()
    return fields


def _change_gate(path: Path) -> str:
    """抽取 amendment 窗口之外的现行变更门。"""
    text = path.read_text(encoding="utf-8")
    start, end = _CHANGE_GATE_BOUNDS[path.name]
    start_at = text.find(start)
    assert start_at >= 0, f"{path.name} 缺少变更门标题：{start}"
    if end is None:
        tail = text[start_at + len(start) :]
        # 边界是 EOF 这件事本身必须被钉住：将来追加尾部章节会静默扩大窗口。
        assert "\n## " not in tail, f"{path.name} 变更门之后出现了未纳入边界的二级章节"
        return text[start_at:]
    end_at = text.find(end, start_at + len(start))
    assert end_at >= 0, f"{path.name} 缺少变更门边界：{end}"
    return text[start_at:end_at]


def _is_owner_approval_permalink(value: str) -> bool:
    """PR 首页 URL 不是批准出处；必须精确到某条 comment 或 review。"""
    return value.startswith(
        "https://github.com/shixian66/xiaowei-agent/pull/"
    ) and ("#issuecomment-" in value or "#pullrequestreview-" in value)


def _has_accepted_owner_approval(path: Path) -> bool:
    fields = _amendment_metadata(path)
    return (
        fields.get("状态") == "Accepted"
        and bool(fields.get("决策人"))
        and fields.get("决策日期") == "2026-09-20"
        and _is_owner_approval_permalink(fields.get("批准出处", ""))
    )


def test_w0_adr_007_records_only_the_redacted_status_read_exception() -> None:
    amendment = _web_product_amendment(_ADR_007)
    assert _has_accepted_owner_approval(_ADR_007)
    # 放开的是「读一个脱敏投影」，不是「读配置」。
    assert "VIEW_INTEGRATION_STATUS" in amendment
    for field in ("域名", "`configured`", "`restart_required`"):
        assert field in amendment
    # 未放开的四类必须逐个点名，不能只写一句「其余不变」。
    for kept in ("原始配置 DTO", "Secret", "配置保存/清除", "probe"):
        assert kept in amendment
    assert "`LOCAL_ADMIN`" in amendment
    assert "W4b" in amendment and "网络调用为 0" in amendment
    assert "W4c" in amendment and "现场 GO" in amendment


def test_w0_adr_007_change_gate_points_to_the_effective_replacement() -> None:
    gate = _change_gate(_ADR_007)
    assert _W0_AMENDMENT_HEADING.removeprefix("## ") in gate, (
        "D5 变更门没有指向 2026-09-20 修订，窗口内外会各有一套现行规则"
    )
    # 关键的不对称：只有「读脱敏状态投影」被窄替代。
    assert "脱敏状态投影" in gate
    for still_absolute in ("写配置", "探针"):
        assert still_absolute in gate
    assert "继续完整生效" in gate


def test_w0_adr_014_replaces_r1_r2_r3_at_their_actual_security_boundaries() -> None:
    amendment = _web_product_amendment(_ADR_014)
    assert _has_accepted_owner_approval(_ADR_014)
    assert "取代 RI5 R1" in amendment
    assert "取代 RI5 R2" in amendment
    assert "取代 RI5 R3" in amendment
    # R2：替代的是先后顺序，不是认证本身。
    assert "loopback" in amendment
    assert "改密前只允许改密和退出" in amendment
    assert "边缘限流" in amendment
    # 当前没有应用层限流这件事不能被「补偿控制」一词盖掉。
    assert "应用层没有 HTTP rate limiter" in amendment
    # R3：只解冻一张表，且 return intent 是闭集。
    assert "`web_oauth_login_contexts`" in amendment
    assert "`state_digest`" in amendment
    for intent in _W0_RETURN_INTENTS:
        assert f"`{intent}`" in amendment
    assert "不保存任意 URL" in amendment
    assert "连接测试 state 不产生" in amendment


def test_w0_adr_014_r2_cites_an_owner_approval_source() -> None:
    fields = _amendment_metadata(_ADR_014)
    assert fields.get("状态") == "Accepted"
    assert _is_owner_approval_permalink(fields.get("批准出处", ""))
    assert fields["批准出处"] == _W0_OWNER_APPROVAL


def test_w0_adr_014_change_gate_points_to_the_effective_replacements() -> None:
    gate = _change_gate(_ADR_014)
    assert _W0_AMENDMENT_HEADING.removeprefix("## ") in gate
    # 三项被取代的门必须逐项带指针，而不是整段照旧。
    for superseded in ("R1", "R2", "R3"):
        assert f"由本修订 {superseded}" in gate
    # 其余原门继续有效。
    assert "新增第三个 Provider" in gate


def test_w0_auth_adr_bindings_are_discriminating() -> None:
    """反例：撤掉替代指针、改宽 D5、或把批准出处换成 PR 首页时必须转红。"""
    gate_007 = _change_gate(_ADR_007)
    widened_d5 = _replace_once(gate_007, "脱敏状态投影", "配置")
    assert "脱敏状态投影" not in widened_d5

    revived = _replace_once(
        gate_007, _W0_AMENDMENT_HEADING.removeprefix("## "), "（无）"
    )
    assert _W0_AMENDMENT_HEADING.removeprefix("## ") not in revived

    gate_014 = _change_gate(_ADR_014)
    dropped_pointer = _replace_once(gate_014, "由本修订 R2", "由后续修订")
    assert "由本修订 R2" not in dropped_pointer

    assert not _is_owner_approval_permalink(
        "https://github.com/shixian66/xiaowei-agent/pull/59"
    )
    assert not _is_owner_approval_permalink("实现者在 PR 描述中转述")
    assert _is_owner_approval_permalink(_W0_OWNER_APPROVAL)

    amendment_014 = _web_product_amendment(_ADR_014)
    without_intent = _replace_once(amendment_014, "`ACTIVATION_STATUS(request_id)`", "任意 URL")
    assert "`ACTIVATION_STATUS(request_id)`" not in without_intent


# --- W0 身份/审计与三域配置门 ----------------------------------------------
#
# 三域挂载矩阵是这一组的重点。散文里写「API 不挂载配置」很容易看起来对，
# 但真正要防的是**表格里多出一格可见性**。所以这里从 Markdown 表解析出
# 每个文件的消费者集合，按集合相等断言——未列出的消费者一律不可见。

_W0_ADMIN_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "MANAGE_USERS",
        "MANAGE_DUTY_BINDINGS",
        "VIEW_ADMIN_AUDIT",
        "VIEW_PRIVATE_TASK_CONTENT",
        "VIEW_INTEGRATION_STATUS",
        "MANAGE_INTEGRATIONS",
        "RUN_CONNECTION_TESTS",
    }
)
_W0_CHANNEL_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {"VIEW_SAFE_TASK", "SUBMIT_READONLY_TASK", "ADMIN_ALL_SAFE_TASKS"}
)
_W0_CONFIG_DOMAIN_MATRIX: Final[dict[str, dict[str, str]]] = {
    ".config/ai/config.json": {"web-app": "读写", "task-worker": "只读"},
    ".config/feishu/config.json": {
        "web-app": "读写",
        "feishu-listener": "只读",
        "channel-worker": "只读",
    },
    ".config/resources/config.json": {"web-app": "读写", "task-worker": "只读"},
}


def _config_domain_matrix(text: str) -> dict[str, dict[str, str]]:
    """从 amendment 的挂载表解析 {配置文件: {消费者: 可见性}}。"""
    matrix: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("| `.config/"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        path = cells[0].strip("`")
        consumers: dict[str, str] = {}
        for entry in cells[1].split("、"):
            consumer, _, visibility = entry.partition("=")
            if visibility:
                consumers[consumer.strip().strip("`")] = visibility.strip()
        matrix[path] = consumers
    return matrix


def test_w0_adr_013_keeps_channel_permissions_closed_and_adds_admin_boundaries() -> None:
    amendment = _web_product_amendment(_ADR_013)
    assert _has_accepted_owner_approval(_ADR_013)
    for role in ("ADMIN", "OPERATOR", "USER"):
        assert f"`{role}`" in amendment
    # 核心权限枚举不因管理面需求膨胀。
    assert "`ChannelPermission` 仍精确三成员" in amendment
    for permission in _W0_CHANNEL_PERMISSIONS:
        assert f"`{permission}`" in amendment
    for capability in _W0_ADMIN_CAPABILITIES:
        assert f"`{capability}`" in amendment
    assert "`MANAGE_INTEGRATIONS`" in amendment and "`RUN_CONNECTION_TESTS`" in amendment
    # Admin 审计与任务审计是两张表，且写不进去就必须拒绝执行。
    assert "`task_audit_events`" in amendment
    assert "append-only" in amendment
    assert "fail-closed" in amendment
    assert "W1a" in amendment and "W1b" in amendment and "W3" in amendment
    # 结果 ACL 不提前。
    assert "requester/approver" in amendment and "R1" in amendment
    assert "不绕过" in amendment


def test_w0_adr_015_freezes_the_future_three_domain_mount_matrix() -> None:
    amendment = _web_product_amendment(_ADR_015)
    assert _has_accepted_owner_approval(_ADR_015)
    assert _config_domain_matrix(amendment) == _W0_CONFIG_DOMAIN_MATRIX
    # 未列出的消费者一律不可见，这三个必须被显式点名。
    for invisible in ("api", "migrate", "postgres"):
        assert f"`{invisible}`" in amendment
    # 未来目标与当前事实必须分开写。
    assert "W4a" in amendment
    assert "`.config/integrations.json`" in amendment
    assert "当前实现" in amendment
    assert "不长期双读" in amendment
    assert "`migration_required`" in amendment
    assert "Web 不取得任务模型 port" in amendment
    assert "本修订不授权" in amendment


def test_w0_adr_015_change_gate_points_to_the_effective_replacement() -> None:
    gate = _change_gate(_ADR_015)
    assert _W0_AMENDMENT_HEADING.removeprefix("## ") in gate
    assert "单配置文件的信任范围" in gate
    # 替换配置文件形态不等于放开供应商边界。
    assert "新增第三个\nProvider 字段族" in gate or "新增第三个 Provider 字段族" in gate
    assert "继续完整生效" in gate


def test_w0_channel_and_config_bindings_are_discriminating() -> None:
    """反例：矩阵多一格可见性、抽掉 fail-closed、或旧门复活时必须转红。"""
    amendment_015 = _web_product_amendment(_ADR_015)

    leaked_to_api = _replace_once(
        amendment_015,
        "| `.config/ai/config.json` | `web-app`=读写、`task-worker`=只读 |",
        "| `.config/ai/config.json` | `web-app`=读写、`task-worker`=只读、`api`=只读 |",
    )
    assert _config_domain_matrix(leaked_to_api) != _W0_CONFIG_DOMAIN_MATRIX

    resources_to_listener = _replace_once(
        amendment_015,
        "| `.config/resources/config.json` | `web-app`=读写、`task-worker`=只读 |",
        "| `.config/resources/config.json` | `web-app`=读写、`feishu-listener`=只读 |",
    )
    assert _config_domain_matrix(resources_to_listener) != _W0_CONFIG_DOMAIN_MATRIX

    amendment_013 = _web_product_amendment(_ADR_013)
    without_fail_closed = _replace_once(amendment_013, "fail-closed", "记录告警后继续")
    assert "fail-closed" not in without_fail_closed

    early_acl = _replace_once(amendment_013, "requester/approver", "（无名单）")
    assert "requester/approver" not in early_acl

    gate_015 = _change_gate(_ADR_015)
    revived = _replace_once(
        gate_015, _W0_AMENDMENT_HEADING.removeprefix("## "), "（无）"
    )
    assert _W0_AMENDMENT_HEADING.removeprefix("## ") not in revived


# --- W0 稳定文档门 --------------------------------------------------------
#
# RI5 早就把 Provider 凭据从 Compose secret 换成了 .config/integrations.json
# （见 docker-compose.yml「Provider 凭据不再走 Docker secret」），但三份当前
# 事实文档里还留着 8 处旧路径。这类漂移的特征是：每一处单独看都像历史描述，
# 合起来却让读者按已退休的方式准备凭据。所以这里按**精确字面量计数为 0**
# 断言，并只扫描当前事实文档——保留历史的 ADR 和测试夹具不在其内。

# 同一事实在这些文档里有多种拼写：精确路径字面量、以及英文短语
# "worker-only / file-backed Compose secret"。只封堵路径字面量时，README 顶部
# 和 handoff 的「已批准边界固定」段照旧全绿——这正是本轮复审打回的那两处。
# 保留 `Compose secret` 本身不入闭集：`postgres_password` 仍然是合法的 Compose
# secret，而 handoff 的历史事故记录（过去时）也应当留得住。
_RETIRED_GEMINI_SECRET_PATHS: Final[tuple[str, ...]] = (
    ".secrets/gemini_api_key",
    "/run/secrets/gemini_api_key",
    "worker-only Compose secret",
    "file-backed Compose secret",
)
_STALE_ARCHITECTURE_CLAIMS: Final[tuple[str, ...]] = (
    "已接受但尚未实现的 RI5 修订",
    "被接受，但**尚未实现**",
)
_CURRENT_TRUTH_DOCS: Final[tuple[str, ...]] = (
    "ARCHITECTURE.md",
    "DEVELOPMENT_PLAN.md",
    "AGENT_HANDOFF.md",
    "README.md",
)
_MAIN_BASELINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"`main@([0-9a-f]{40})`")
"""handoff 里引用某个 `main` 基线的写法。"""

_CURRENT_BASELINE_MARKER: Final[str] = "- **当前基线**："
"""当前基线的唯一规范出处。

不写死某一轮的 SHA：那样的守卫只在写它的那一轮为真，下一轮必然误报，
而误报的守卫最终会被放宽——放宽之后它就再也抓不到真正的漂移了。

也不能只要求"第 1 节的 SHA 在第 0 节出现过"：第 0 节记录的是基线**历史**，
每一轮的 SHA 都留在那里，于是任何一个陈旧 SHA 都能满足它。必须绑定到
**单一规范出处**，两处才真的会一起动。
"""


def _current_baseline(text: str) -> str:
    """只接受唯一的当前基线声明，历史 SHA 不能冒充它。"""
    declarations = [line for line in text.splitlines() if line.startswith(_CURRENT_BASELINE_MARKER)]
    assert len(declarations) == 1, "handoff 必须恰有一条当前基线声明"
    found = _MAIN_BASELINE_PATTERN.findall(declarations[0])
    assert len(found) == 1, "当前基线必须恰好一个完整 SHA"
    return found[0]
_W0_WEB_STAGES: Final[tuple[str, ...]] = (
    "W0",
    "W1a",
    "W1b",
    "W2",
    "W3",
    "W4a",
    "W4b",
    "W5",
)


def _truth_doc_text(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def test_truth_docs_do_not_revive_the_retired_gemini_secret_path() -> None:
    for name in _CURRENT_TRUTH_DOCS:
        text = _truth_doc_text(name)
        for retired in _RETIRED_GEMINI_SECRET_PATHS:
            assert text.count(retired) == 0, (
                f"{name} 仍把已退休的 {retired} 写成当前 Provider 凭据真源"
            )
        assert "`.config/integrations.json`" in text


def test_w0_stable_docs_distinguish_current_runtime_from_future_targets() -> None:
    architecture = _truth_doc_text("ARCHITECTURE.md")
    for stale in _STALE_ARCHITECTURE_CLAIMS:
        assert architecture.count(stale) == 0, f"ARCHITECTURE.md 仍含过期声称：{stale}"
    # 当前事实：W4a 三域已离线实现，证据等级到 tests 为止；旧单文件只作迁移输入。
    for domain_path in (
        "/run/xiaowei-config/ai/config.json",
        "/run/xiaowei-config/feishu/config.json",
    ):
        assert domain_path in architecture
    for retired in _W4A_RETIRED_SINGLE_FILE_TRUTH:
        assert retired not in architecture, f"ARCHITECTURE.md 仍把旧单文件写成当前真源：{retired}"
    assert "migration_required" in architecture
    assert "W4a" in architecture and "W5" in architecture


def test_development_plan_orders_web_stages_and_keeps_gates_outside() -> None:
    plan = _truth_doc_text("DEVELOPMENT_PLAN.md")
    sequence = _section_between(
        plan,
        start="### Web 产品线交付序列（V2.5 新增）",
        end="### 独立阻塞门（不在上述必经序列内）",
    )
    positions = []
    for stage in _W0_WEB_STAGES:
        at = sequence.find(f"**{stage} ")
        assert at >= 0, f"DEVELOPMENT_PLAN.md 缺少 Web 产品阶段 {stage}"
        positions.append(at)
    assert positions == sorted(positions), "W0–W5 顺序不是文档中的实际先后"
    # W4c 与 R1 是独立阻塞门，不得混进必经序列。
    assert "**W4c " not in sequence and "**R1 " not in sequence
    gates = plan[plan.find("### 独立阻塞门（不在上述必经序列内）") :]
    assert "W4c" in gates and "R1" in gates
    assert "Approved V2.6" in plan
    assert _W0_OWNER_APPROVAL in plan
    # I3 延期不等于取消。
    assert "I3" in plan and "延期" in plan


_W4A_DOMAIN_FILES: Final[tuple[str, ...]] = (
    "`.config/ai/config.json`",
    "`.config/feishu/config.json`",
)
_W4A_RETIRED_SINGLE_FILE_TRUTH: Final[tuple[str, ...]] = (
    "唯一真源是 `.config/integrations.json`",
    "唯一真源是宿主 Git-ignored 的 `.config/integrations.json`",
    "仍是单一\n`.config/integrations.json`",
    "/run/xiaowei-config/integrations.json` 出现",
)


def _readme_runbook(readme: str) -> str:
    runbook_at = readme.find("Compose 启动前只需要准备一个已被 Git 忽略的本地文件")
    assert runbook_at >= 0
    return readme[runbook_at : runbook_at + 6000]


def test_readme_runbook_uses_the_w4a_domains_and_an_explicit_migration() -> None:
    """W4a 起当前首启步骤就是三域形态；旧单文件只作为显式迁移的输入出现。"""
    readme = _truth_doc_text("README.md")
    runbook = _readme_runbook(readme)
    for domain_file in _W4A_DOMAIN_FILES:
        assert domain_file in runbook, f"首启步骤缺少三域文件 {domain_file}"
    # W4b 起 resources 域有了固定文件；W4a 时这里只是一个预留目录。
    assert "`.config/resources/config.json`" in runbook
    # 旧文件只能经显式 CLI 迁移，且必须先停消费者；运行时不双读、不自动迁移。
    assert "python -m xiaowei_agent.interfaces.integration_config_migrate" in runbook
    assert "migration_required" in runbook
    assert "先停止" in runbook
    for retired in _W4A_RETIRED_SINGLE_FILE_TRUTH:
        assert retired not in readme, f"README 仍把旧单文件写成当前真源：{retired}"
    # 离线实现不等于迁移已执行或已部署。
    for overclaim in ("迁移已执行", "已完成迁移", "已部署"):
        assert overclaim not in runbook
    # 已批准的产品演进要能从 README 导航到。
    assert "2026-09-19-web-operations-console-identity-activation-design.md" in readme
    assert "W0" in readme and "W1a" in readme


def test_readme_w4a_runbook_guard_is_discriminating() -> None:
    readme = _truth_doc_text("README.md")
    revived = _replace_once(
        readme,
        "Compose 启动前只需要准备一个已被 Git 忽略的本地文件",
        "Compose 启动前只需要准备一个已被 Git 忽略的本地文件；"
        "唯一真源是 `.config/integrations.json`",
    )
    assert any(retired in revived for retired in _W4A_RETIRED_SINGLE_FILE_TRUTH)
    runbook = _readme_runbook(readme)
    assert all(domain_file in runbook for domain_file in _W4A_DOMAIN_FILES)
    stripped = runbook.replace("`.config/feishu/config.json`", "")
    assert not all(domain_file in stripped for domain_file in _W4A_DOMAIN_FILES)


def test_w0_stable_doc_bindings_are_discriminating() -> None:
    """反例：恢复旧 Secret 路径、换阶段顺序、或把 W4c 插进必经序列必须转红。"""
    architecture = _truth_doc_text("ARCHITECTURE.md")
    revived = architecture + "\n宿主 key 固定为 `.secrets/gemini_api_key`。\n"
    assert any(revived.count(path) > 0 for path in _RETIRED_GEMINI_SECRET_PATHS)

    plan = _truth_doc_text("DEVELOPMENT_PLAN.md")
    swapped = _replace_once(plan, "**W1a ", "«W1a-moved» ")
    assert swapped.find("**W1a ") < 0

    with_gate_inline = _replace_once(plan, "**W4b ", "**W4c 独立门** 与 **W4b ")
    sequence_start = with_gate_inline.find("**W0 ")
    sequence_end = with_gate_inline.find("### 独立阻塞门", sequence_start)
    assert "**W4c " in with_gate_inline[sequence_start:sequence_end]


# --- W0 交接门 ------------------------------------------------------------
#
# handoff 最容易出的错不是写少，而是**把批准写成实现**：设计合入了、CI 绿了、
# 计划批了，读起来就像 W1a 已经在跑。这里把「有什么证据」和「下一步只许做
# 什么」分开钉死，并要求批准来源是评论永久链接而不是 merge 事实本身。

_W0_FORBIDDEN_HANDOFF_CLAIMS: Final[tuple[str, ...]] = (
    "真实飞书已可用",
    "Web 产品已部署",
    "已用户验收",
    "已 canary",
)


def test_w0_handoff_records_design_merge_and_keeps_implementation_claims_closed() -> None:
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    # 设计合入事实必须精确到 PR 与 merge SHA。
    assert "PR #58" in handoff
    assert "4e5a844620b700e25d6a29e43687c1a4c876db16" in handoff
    # 批准来源是评论永久链接，不是「已合入」这件事。
    assert _W0_OWNER_APPROVAL in handoff
    assert "2026-09-20" in handoff
    for claim in _W0_FORBIDDEN_HANDOFF_CLAIMS:
        assert claim not in handoff, f"handoff 出现越级声称：{claim}"
    # RI5 的实现状态必须是当前口径。
    assert "未来 Admin 配置治理无源码" not in handoff
    # 独立真实调用门保持关闭。
    for gate in ("RI2", "RI3", "RI4", "RI6", "E1"):
        assert gate in handoff


def _handoff_baseline_field(text: str, name: str) -> str:
    """按字段名取第 1 节基线表的值。

    旧断言只排除一句带粗体的散文，于是同一事实换成表格字段就能漏过去。
    """
    section = _section_between(
        text, start="## 1. 当前基线", end="## 2. 已确认的设计口径"
    )
    for line in section.splitlines():
        row = re.match(rf"^\| *{re.escape(name)} *\| *(.+?) *\|$", line)
        if row:
            return row.group(1)
    raise AssertionError(f"AGENT_HANDOFF.md 第 1 节缺少字段：{name}")


def _next_step_statements(text: str) -> list[str]:
    """收集全文每一处「下一步」声明——表格字段和散文句都算。

    只认 `下一步是` / `下一件事是` 这种**声明式**句子；归档规则里把「下一步」
    当字段名列举的那类句子和小节标题不在其内。
    """
    statements: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        row = re.match(r"^\| *(下一步|阶段) *\| *(.+?) *\|$", line)
        if row:
            statements.append(row.group(2))
            continue
        if re.search(r"(下一步|下一件事)是", line):
            statements.append(line)
    return statements


def test_handoff_baseline_table_references_the_single_declaration() -> None:
    """表格直接指向唯一声明，不能复制 SHA 或机器专属路径。"""
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    project_dir = _handoff_baseline_field(handoff, "项目目录")
    assert "/Users/" not in project_dir
    assert not _MAIN_BASELINE_PATTERN.findall(project_dir)
    assert "(#current-baseline)" in project_dir
    assert handoff.count('<a id="current-baseline"></a>') == 1
    assert len(_current_baseline(handoff)) == 40


def test_handoff_baseline_declaration_guard_is_discriminating() -> None:
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    current = _current_baseline(handoff)
    declaration = f"{_CURRENT_BASELINE_MARKER}`main@{current}`"
    with pytest.raises(AssertionError):
        _current_baseline(handoff + "\n" + declaration)
    with pytest.raises(AssertionError):
        _current_baseline(handoff.replace(_CURRENT_BASELINE_MARKER, "历史基线："))
    drifted = _replace_once(handoff, f"`main@{current}`", "`main@short`")
    with pytest.raises(AssertionError):
        _current_baseline(drifted)


def test_handoff_retains_web_stages_and_i3_deferral() -> None:
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    assert "W0" in handoff and "W1a" in handoff
    assert "I3" in handoff and "延期" in handoff


def test_w0_handoff_binding_is_discriminating() -> None:
    """反例：加入任一越级声称、删除批准来源或恢复 I3 唯一下一步都必须转红。"""
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    for claim in _W0_FORBIDDEN_HANDOFF_CLAIMS:
        polluted = f"{handoff}\n- {claim}。\n"
        assert claim in polluted

    without_source = _replace_once(handoff, _W0_OWNER_APPROVAL, "见 PR #58 已合入")
    assert _W0_OWNER_APPROVAL not in without_source

    # 反例必须走真正的解析器，而不是断言自己刚拼上去的字符串。
    # 本轮复审打回的就是这一点：旧断言只排除一句散文，换成表格字段就漏过去。
    revived_i3 = _replace_once(
        handoff,
        _handoff_baseline_field(handoff, "下一步"),
        "另起 I3 受治理资料查询计划，先拍板资料源形态",
    )
    reverted = [
        statement
        for statement in _next_step_statements(revived_i3)
        if "I3" in statement and "延期" not in statement and "未取消" not in statement
    ]
    assert reverted, "把下一步表格字段改回 I3 之后，结构化解析必须能看见它"

    machine_path = _replace_once(
        handoff,
        "| 项目目录 | 当前开发分支",
        "| 项目目录 | 当前在 worktree `/Users/someone/agent`，当前开发分支",
    )
    assert "/Users/" in _handoff_baseline_field(machine_path, "项目目录")

    for retired in _RETIRED_GEMINI_SECRET_PATHS:
        assert handoff.count(retired) == 0
    readme = _truth_doc_text("README.md")
    assert "worker-only Compose secret" not in readme
    revived_topology = readme + "\nworker-only Compose secret\n"
    assert any(
        revived_topology.count(retired) > 0 for retired in _RETIRED_GEMINI_SECRET_PATHS
    )


# --- 凭据可见性 vs 模型调用范围：角色不变量 --------------------------------
#
# 上一轮把守卫做成了「禁止短语闭集」。黑名单只能挡住已知的错法，挡不住
# 新造的错法——修复时写出的 `worker-only 凭据可见性` 就是第五种错误表述，
# 95 passed 全绿放行。这里改成绑定**角色不变量**本身：
#
#   凭据/配置文件：web-app 读写，worker/feishu-listener/channel-worker 只读，
#                  api/migrate/postgres 不挂载；Web 为配置管理和显式连接测试读 Secret。
#   模型调用端口：`IntentModelPort` / `SlowQueryAdvisoryPort` 才是 task-worker-only。
#
# 因此「worker 独占」这个说法只允许修饰模型调用端口，不允许修饰凭据或配置
# 文件。Compose 侧的挂载矩阵已由 tests/security/test_ri5_compose_boundary.py
# 承重，这里不重复校验，只管文档有没有把两个角色说混。

_WORKER_EXCLUSIVITY_MARKERS: Final[tuple[str, ...]] = (
    "worker-only",
    "只对 task worker",
    "只挂给 worker",
    "只挂载给 worker",
    "只挂给 task worker",
    "仅 task worker",
    "只由 task worker",
    "只给 worker",
)
_CREDENTIAL_NOUNS: Final[tuple[str, ...]] = (
    "Secret",
    "凭据",
    "key",
    "Key",
    "integrations.json",
    "config.json",
    "gemini_api_key",
)
_MODEL_PORT_NOUNS: Final[tuple[str, ...]] = (
    "IntentModelPort",
    "SlowQueryAdvisoryPort",
    "模型调用",
    "模型端口",
)
# 历史事故/交付记录可以留着原文，但必须自带替代指针——与四份 ADR 的
# 「历史原文保留 + 替代指针」同一套办法，不为文档好看而改写历史。
_SUPERSEDED_MARKER: Final[str] = "已由 RI5 取代"


def _credential_scope_conflations(text: str) -> list[str]:
    """找出把 worker 独占安到凭据/配置文件头上的句子。"""
    conflations: list[str] = []
    for line in text.splitlines():
        if not any(marker in line for marker in _WORKER_EXCLUSIVITY_MARKERS):
            continue
        if not any(noun in line for noun in _CREDENTIAL_NOUNS):
            continue
        if any(noun in line for noun in _MODEL_PORT_NOUNS):
            continue  # 修饰的是模型调用端口，正确用法
        if _SUPERSEDED_MARKER in line:
            continue  # 历史记录且已标注被取代
        conflations.append(line.strip())
    return conflations


def test_truth_docs_scope_worker_exclusivity_to_model_ports_only() -> None:
    for name in _CURRENT_TRUTH_DOCS:
        conflations = _credential_scope_conflations(_truth_doc_text(name))
        assert not conflations, (
            f"{name} 把 worker 独占安在了凭据/配置文件上；"
            f"RI5 起 Web 也读 Secret（配置管理与显式连接测试），"
            f"只有模型调用端口是 task-worker-only：{conflations}"
        )


def test_truth_docs_state_that_the_web_plane_also_reads_the_credential() -> None:
    """正向不变量：光靠否定句挡不住漏写，得要求真源把 web-app 的角色写出来。"""
    architecture = _truth_doc_text("ARCHITECTURE.md")
    # W4a 起按域写出：web-app 三域读写，api 与其余非消费者一个域都不挂。
    assert "`web-app` 三域读写" in architecture
    assert "`api` / `migrate` / `postgres` 不挂任何域" in architecture


def test_credential_scope_binding_is_discriminating() -> None:
    """反例：任何新造的「凭据 worker 独占」说法都要被抓住，不只是已知那几种。"""
    architecture = _truth_doc_text("ARCHITECTURE.md")

    # 第五种表述——上一轮正是它全绿溜过去的。
    invented = "Gemini 凭据采用 worker-only 可见性。"
    assert _credential_scope_conflations(invented)

    # 第六种，换个词照样抓。
    another = "宿主 key 文件只挂给 worker。"
    assert _credential_scope_conflations(another)

    # 正确用法不误伤：worker 独占修饰的是模型调用端口。
    correct = "Gemini 模型调用端口只由 task worker 装配，Web 不获得该端口。"
    assert not _credential_scope_conflations(correct)

    # 带替代指针的历史记录不误伤。
    historical = f"当时改为宿主 key 文件到 worker-only file-backed secret（{_SUPERSEDED_MARKER}）。"
    assert not _credential_scope_conflations(historical)

    without_web_role = _replace_once(architecture, "`web-app` 三域读写", "三域只读挂载")
    assert "`web-app` 三域读写" not in without_web_role


# --- W1a 写内核：实现状态与它的天花板 --------------------------------------
#
# 三个切片全部合入之后，真源文档里"W1a 还没有源码"那句话就是**假的**，而四门
# 依旧全绿——这正是验收假绿的形状：代码变了、文档没变，没有任何东西会失败。
# 所以这一组同时钉两头：已经成立的事实必须写出来（否则下一位实现者会重做一遍），
# 还没成立的事实不许被写成已经成立（否则 W1b 会被当作已交付）。

_W1A_ARCHITECTURE_FACTS: Final[tuple[str, ...]] = (
    "`user_accounts`",
    "`user_role_assignments`",
    "`external_identities`",
    "`admin_audit_events`",
    "`rev_0014`",
    "`UserDirectoryStore.apply()`",
    "`AdminAuditStore`",
)
_W1A_SECTION_START: Final[str] = "**用户目录与 Admin 审计契约（W1a）**"
_W1A_SECTION_END: Final[str] = "**身份激活契约（W1b）**"


def _w1a_architecture_section() -> str:
    return _section_between(
        _truth_doc_text("ARCHITECTURE.md"),
        start=_W1A_SECTION_START,
        end=_W1A_SECTION_END,
    )


def test_w1a_contract_and_current_evidence_have_separate_owners() -> None:
    section = _w1a_architecture_section()
    for fact in _W1A_ARCHITECTURE_FACTS:
        assert fact in section, f"ARCHITECTURE.md 的 W1a 契约缺少：{fact}"
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    assert "W1a 写内核与 W1b 两个切片已离线实现" in handoff
    assert "`tests`" in handoff


def test_architecture_states_the_single_write_path_invariant() -> None:
    """单一写路径与同事务审计是 W1a 的目的本身，不能只留在计划里。"""
    section = _w1a_architecture_section()
    assert "唯一写入口" in section
    assert "同一个事务" in section
    assert "从命令派生" in section


_ACTIVATION_NOUNS: Final[tuple[str, ...]] = (
    "激活流程",
    "激活请求",
    "ActivationRequest",
    "ActivationStore",
    "IdentityActivationService",
    "web_oauth_login_contexts",
    "登录改造",
    "登录页",
    "Admin 页面",
    "审计查询 API",
)
_EXISTENCE_MARKERS: Final[tuple[str, ...]] = (
    "已实现",
    "已交付",
    "已上线",
    "已可用",
    "已部署",
    "已验收",
    "已完成",
)
# 带阶段名或否定词的句子讲的是**未来**或**不存在**，不是声称它已经在跑。
# 没有这条豁免，真源就写不出「W1a 已实现，W1b 的激活流程尚未开始」这句真话。
_FUTURE_SCOPE_MARKERS: Final[tuple[str, ...]] = (
    "W1b",
    "W2",
    "W3",
    "W4a",
    "W4b",
    "W5",
    "尚未",
    "未开始",
    "未授权",
    "不得",
    "没有",
    "不是",
    "将",
    "留给",
)


def _activation_existence_claims(text: str) -> list[str]:
    """找出把激活流程 / 登录改造 / Admin 页面写成已经存在的句子。

    按**性质**判而不是按拼写：任一激活面名词 + 任一存在标记 = 声称它在跑，除非
    同一句里带着阶段名或否定词。黑名单式的"禁止短语闭集"挡不住新造的说法。
    """
    claims: list[str] = []
    for line in text.splitlines():
        if not any(noun in line for noun in _ACTIVATION_NOUNS):
            continue
        if not any(marker in line for marker in _EXISTENCE_MARKERS):
            continue
        if any(marker in line for marker in _FUTURE_SCOPE_MARKERS):
            continue
        claims.append(line.strip())
    return claims


def test_truth_docs_do_not_claim_activation_or_admin_ui_exists() -> None:
    for name in _CURRENT_TRUTH_DOCS:
        claims = _activation_existence_claims(_truth_doc_text(name))
        assert not claims, (
            f"{name} 把 W1b/W2 的激活面写成了已经存在：{claims}"
        )


def test_the_activation_claim_guard_is_discriminating() -> None:
    """反例：那条守卫不能宽到什么都抓不到。"""
    assert _activation_existence_claims("Admin 页面已上线，管理员可以直接改角色。")
    # 新造的说法照样抓，不依赖已知短语闭集。
    assert _activation_existence_claims("身份激活流程已完成并对全部租户可用。")


def test_naming_a_future_component_with_its_phase_is_allowed() -> None:
    """正常对照：写"W1b 将提供 X"合法，否则真源根本无法描述下一步。"""
    assert not _activation_existence_claims(
        "W1a 写内核已实现；W1b 将提供激活流程，本阶段没有它的源码。"
    )
    assert not _activation_existence_claims("激活流程尚未实现，留给 W1b。")


# --- W1b 离线激活流程：实现状态与真实渠道硬门 ------------------------------

_W1B_ARCHITECTURE_FACTS: Final[tuple[str, ...]] = (
    "`activation_requests`",
    "`rev_0015`",
    "`IdentityActivationService`",
    "`DirectoryFeishuIdentityDirectory`",
    "`activation_pending`",
    "`ActivationNotificationService`",
)
_W1B_SECTION_START: Final[str] = "**身份激活契约（W1b）**"
_W1B_SECTION_END: Final[str] = "**产品边界与阶段归属**"


def _w1b_architecture_section() -> str:
    return _section_between(
        _truth_doc_text("ARCHITECTURE.md"),
        start=_W1B_SECTION_START,
        end=_W1B_SECTION_END,
    )


def test_w1b_contract_and_handoff_keep_live_evidence_separate() -> None:
    section = _w1b_architecture_section()
    for fact in _W1B_ARCHITECTURE_FACTS:
        assert fact in section, f"ARCHITECTURE.md 的 W1b 契约缺少：{fact}"
    assert "数据库目录" in section
    assert "单次" in section
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    assert "已离线实现" in handoff and "`tests`" in handoff
    for unverified in ("真实飞书", "部署", "canary", "用户验收"):
        assert unverified in handoff


def test_handoff_preserves_w1b_prerequisite_after_w2_closure() -> None:
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    assert "W1b 两个切片已离线实现" in handoff
    assert "激活流程" in handoff and "单次群通知" in handoff
    assert "W2 离线范围已完成并收口" in handoff


_RETIRED_README_W1A_PHRASES: Final[tuple[str, ...]] = (
    "W1a 详细实施计划送审",
    "计划获批后才可开始",
    "没有 `UserAccount`",
    "任何 W1a 源码",
)


def _readme_status() -> str:
    return _section_between(
        _truth_doc_text("README.md"), start="> 当前状态：", end="## 先看什么"
    )


def test_readme_status_links_to_handoff_without_stale_w1a_claims() -> None:
    status = _readme_status()
    _check_current_status_reference(status, _truth_doc_text("AGENT_HANDOFF.md"))
    for phrase in _RETIRED_README_W1A_PHRASES:
        assert phrase not in status, f"README 顶部仍写着 W1a 没有源码：{phrase}"


def test_the_readme_guard_is_discriminating() -> None:
    """反例：把 README 那段改回旧措辞，上一条必须能看见它。

    反例走的是真正的取段函数，不是断言自己刚拼上去的字符串——后者在取段函数
    写错时同样全绿。
    """
    readme = _truth_doc_text("README.md")
    status = _readme_status()
    reverted_status = status + "> 当前阶段是 W1a 详细实施计划送审，计划获批后才可开始实现。\n"
    reverted = _replace_once(readme, status, reverted_status)

    refetched = _section_between(
        reverted, start="> 当前状态：", end="## 先看什么"
    )
    present = [
        phrase for phrase in _RETIRED_README_W1A_PHRASES if phrase in refetched
    ]
    assert present, "改回旧措辞之后，README 守卫必须能看见退回的字面量"


_HANDOFF_PROGRESS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"main@[0-9a-f]{40}"),
    re.compile(r"PR #\d+"),
    re.compile(r"\d+ passed"),
)


def test_development_plan_carries_no_implementation_progress() -> None:
    """`AGENTS.md:155`：进度与证据只放 handoff，计划只写顺序、门与退出标准。

    日常执行细则可以改为引用 AGENTS，但阶段进度仍只归 handoff。
    """
    plan = _truth_doc_text("DEVELOPMENT_PLAN.md")
    for pattern in _HANDOFF_PROGRESS_PATTERNS:
        found = pattern.findall(plan)
        assert not found, f"DEVELOPMENT_PLAN.md 出现只应放 handoff 的进度证据：{found}"
    # 同一批证据在 handoff 里必须找得到，否则这条守卫只是在禁止一类空模式。
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    assert all(pattern.search(handoff) for pattern in _HANDOFF_PROGRESS_PATTERNS)


def test_handoff_closes_w3_v1_and_moves_deferred_items_out_of_the_w4_gate() -> None:
    """负责人已把 W3-lite 定为 W3 V1；延期增强不能继续阻塞 W4。"""
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    for fact in (
        "17462b7f6c38549818081b5a704f6ec381dea1e7",
        "4e488a48d6b0fe483ad3714970aecc7acc44c954",
        "c391caf3f44f370f5e8a4d53478589a19a001b29",
        "72201a8718a00c7f1bb0828056cbb20b836e1f48",
        "35957857929",
        "4892 passed",
    ):
        assert fact in handoff, f"W3-lite 收口缺少稳定证据：{fact}"
    for stale in (
        "W3 尚无获批详细计划",
        "只能先编写并送审 W3 详细计划",
        "W3 尚未开始",
        "W3-lite 正在交付",
        "切片 B 为待复审候选",
        "仍待最终 exact-SHA",
        "产品下一步是对 PR #78",
    ):
        assert stale not in handoff, f"handoff 仍含 W3-lite 过期口径：{stale}"

    assert "W3 V1 已按精简范围离线完成并收口" in handoff
    assert "W3 后续增强" in handoff
    assert "不再作为 W4a/W4b 的进入条件" in handoff
    assert "真实飞书" in handoff and "用户验收" in handoff
    assert "当前精简产品路线" not in handoff
    assert "独立复审内容未在 GitHub PR 中持久留存" in handoff

    statements = _next_step_statements(handoff)
    assert statements, "handoff 没有任何可解析的下一步声明"
    for statement in statements:
        assert "PR #78" not in statement, f"下一步仍指向已合入的 W3-lite 候选：{statement}"
        if "I3" in statement:
            assert "延期" in statement or "未取消" in statement, (
                f"下一步仍把 I3 写成当前动作：{statement}"
            )

    next_step = _handoff_baseline_field(handoff, "下一步")
    # W5-A 合入后，延期的 W3 增强仍不应把当前动作从 W5-B 候选审查拉回去。
    assert "W5-B" in next_step and "exact-SHA" in next_step
    assert "W3" not in next_step and "GO" in next_step

    development_plan = _truth_doc_text("DEVELOPMENT_PLAN.md")
    web_spec = _truth_doc_text(
        "docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md"
    )
    channel_adr = _truth_doc_text("docs/adr/ADR-013-m7-channel-boundary.md")
    for stable_truth in (development_plan, web_spec, channel_adr):
        assert "W3 V1" in stable_truth
        assert "W3 后续增强" in stable_truth
        assert "不作为 W4a/W4b" in stable_truth


def test_w3_lite_admin_identity_boundary_is_stable_documentation() -> None:
    """稳定文档必须描述能力与边界，而不是只在在途计划里留一份副本。"""
    architecture = _truth_doc_text("ARCHITECTURE.md")
    channel_adr = _truth_doc_text("docs/adr/ADR-013-m7-channel-boundary.md")
    development_plan = _truth_doc_text("DEVELOPMENT_PLAN.md")
    readme = _truth_doc_text("README.md")
    web_spec = _truth_doc_text(
        "docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md"
    )

    for route in (
        "/admin/api/users",
        "/admin/api/activations",
        "/admin/api/audit",
    ):
        assert route in architecture
        assert route in readme

    for fact in (
        "UserDirectoryStore.apply()",
        "配置写入与连接测试继续只允许本地 Admin",
        "Admin 不绕过结果 ACL",
        "普通用户仍只通过具体任务或结果链接进入",
    ):
        assert fact in architecture, f"W3-lite 稳定边界缺失：{fact}"

    for stable_truth in (channel_adr, development_plan, web_spec):
        assert "受管授权变更" in stable_truth
        assert "不新增第二条授权写路径" in stable_truth


# ---------------------------------------------------------------------------
# W1b 计划送审期间暴露的真源冲突：同一张表被两份已批准文档分给了不同阶段。
# 两处单独看都言之成理，而实现者只会读到其中一份——这正是切片 C 那一组
# `subject_ref` 上限的同一个形状：**同一件事被写了两遍，副本可以静默分叉**。
# ---------------------------------------------------------------------------

_LOGIN_CONTEXT_TABLE: Final[str] = "web_oauth_login_contexts"
_W2_PLAN = (
    _ROOT
    / "docs/superpowers/plans/2026-09-23-w2-login-and-multi-shell.md"
)
_STAGE_LABEL = re.compile(r"\*\*(W[0-9][ab]?)\b")


def _stage_owning(text: str, token: str) -> set[str]:
    """取全部"阶段清单条目"里提到 ``token`` 的那些条目的阶段号。

    只认以 `- **W…**` 或 `N. **W…**` 开头的清单条目，散文提及不算——散文里
    自然会同时出现多个阶段名。条目常常换行续写，因此必须先把续行并回条目，
    再判断 ``token`` 落在哪一条：按行判断会漏掉写在第二行的那一半。
    """
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:-|\d+\.)\s+\*\*W[0-9]", stripped):
            items.append(stripped)
        elif items and line.startswith((" ", "\t")) and stripped:
            items[-1] += " " + stripped
        elif not stripped:
            continue
        else:
            items.append("")
    owners: set[str] = set()
    for item in items:
        if not item or token not in item:
            continue
        label = _STAGE_LABEL.search(item)
        assert label is not None, f"阶段条目没有可解析的阶段号：{item}"
        owners.add(label.group(1))
    return owners


def test_the_login_context_table_belongs_to_exactly_one_stage() -> None:
    """`web_oauth_login_contexts` 在两份真源里必须属于同一个阶段。

    冲突的代价不是措辞难看：实现者按详细规格做就会在 W1b 建这张表，按
    `DEVELOPMENT_PLAN.md` 做就不会，而两边都能自称照批准文档执行。
    """
    plan_owners = _stage_owning(
        _truth_doc_text("DEVELOPMENT_PLAN.md"), _LOGIN_CONTEXT_TABLE
    )
    spec_owners = _stage_owning(
        _WEB_PRODUCT_SPEC.read_text(encoding="utf-8"), _LOGIN_CONTEXT_TABLE
    )
    assert plan_owners, "DEVELOPMENT_PLAN.md 的阶段清单不再提到登录 context 表"
    assert spec_owners, "详细规格的交付序列不再提到登录 context 表"
    assert plan_owners == spec_owners == {"W2"}, (
        f"登录 context 表的阶段归属分叉：DEVELOPMENT_PLAN={sorted(plan_owners)}、"
        f"规格={sorted(spec_owners)}"
    )


def test_the_stage_ownership_guard_is_discriminating() -> None:
    """反例：把规格里的归属改回 W1b，上一条必须看得见。

    反例走的是真正的取值函数，而不是断言刚拼出来的字符串——后者在取值函数
    写错时同样全绿。
    """
    spec = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    owners = _stage_owning(spec, _LOGIN_CONTEXT_TABLE)
    assert owners == {"W2"}

    drifted = spec.replace("**W2 登录与页面壳**", "**W1b 激活内核**")
    assert drifted != spec, "反例没有改动任何阶段条目"
    assert _stage_owning(drifted, _LOGIN_CONTEXT_TABLE) != {"W2"}


def _w2_role_intent_matrix(text: str) -> dict[tuple[str, str], tuple[str, str]]:
    """解析 W2 的服务端终态矩阵；每个角色/来源与 intent 只能出现一次。"""
    section = _section_between(
        text,
        start="#### 服务端终态矩阵",
        end="#### 管理中心投影",
    )
    rows: dict[tuple[str, str], tuple[str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 4, line
        key = (cells[0], cells[1])
        assert key not in rows, f"W2 角色×intent 终态重复：{key}"
        rows[key] = (cells[2], cells[3])
    return rows


def test_w2_plan_role_intent_matrix_is_total_and_terminal() -> None:
    """已认证主体不能落入「登录成功但不知道去哪里」的未定义状态。"""
    assert _w2_role_intent_matrix(_W2_PLAN.read_text(encoding="utf-8")) == {
        ("LOCAL_ADMIN", "WORKBENCH"): ("issue_session", "/app"),
        ("LOCAL_ADMIN", "SAFE_TASK_DETAIL"): ("issue_session", "task_acl"),
        ("LOCAL_ADMIN", "ADMIN_CENTER"): ("issue_session", "/admin_local"),
        ("LOCAL_ADMIN", "ACTIVATION_STATUS"): ("deny_no_session", "forbidden"),
        ("FEISHU_ADMIN", "WORKBENCH"): ("issue_session", "/app"),
        ("FEISHU_ADMIN", "SAFE_TASK_DETAIL"): ("issue_session", "task_acl"),
        ("FEISHU_ADMIN", "ADMIN_CENTER"): ("issue_session", "/admin_redacted"),
        ("FEISHU_ADMIN", "ACTIVATION_STATUS"): (
            "subject_bound",
            "restore_original_intent",
        ),
        ("FEISHU_OPERATOR", "WORKBENCH"): ("issue_session", "/app"),
        ("FEISHU_OPERATOR", "SAFE_TASK_DETAIL"): (
            "issue_session",
            "task_acl",
        ),
        ("FEISHU_OPERATOR", "ADMIN_CENTER"): (
            "deny_no_session",
            "destination_not_available",
        ),
        ("FEISHU_OPERATOR", "ACTIVATION_STATUS"): (
            "subject_bound",
            "restore_original_intent",
        ),
        ("FEISHU_USER", "WORKBENCH"): (
            "deny_no_session",
            "destination_not_available",
        ),
        ("FEISHU_USER", "SAFE_TASK_DETAIL"): ("issue_session", "task_acl"),
        ("FEISHU_USER", "ADMIN_CENTER"): (
            "deny_no_session",
            "destination_not_available",
        ),
        ("FEISHU_USER", "ACTIVATION_STATUS"): (
            "subject_bound",
            "restore_original_intent",
        ),
    }


def test_w2_plan_closes_enum_migration_and_single_source_surfaces() -> None:
    """W2 新成员必须贯穿契约、DDL、共享套件与既有单真源。"""
    text = _W2_PLAN.read_text(encoding="utf-8")
    for required in (
        "ActivationSource 全集相等",
        "ActivationStatus 全集相等",
        "DROP CONSTRAINT",
        "ADD CONSTRAINT",
        "`ACTIVATION_REQUESTS` 加入 `_ALTERED_AFTER_CREATION`",
        "`SAFE_TASK_LINK` + 任一群摘要必须拒绝",
        "`web_task_detail_path()` 从 `web_models.py` 移到 `interfaces/web_navigation.py`",
        "`web_models.py` 只导入并复用该函数",
        "`src/xiaowei_agent/_conformance.py`",
        "文件 `rev_0016_web_login_contexts.py` / revision id `0016_web_login_contexts`",
    ):
        assert required in text, f"W2 计划缺少承重要求：{required}"
    stale_conformance_path = (
        "persistence/{web_session.py,schema.py,rows.py,memory.py,fake.py,postgres.py,"
        "_conformance.py}"
    )
    assert stale_conformance_path not in text


def test_w2_plan_resolves_login_window_admin_projection_and_slice_boundaries() -> None:
    """迁移窗口、脱敏投影与机械搬迁不能留到实现者临场选择。"""
    plan = _W2_PLAN.read_text(encoding="utf-8")
    spec = _WEB_PRODUCT_SPEC.read_text(encoding="utf-8")
    for required in (
        "清空现有 `web_oauth_states`",
        "缺登录 context",
        "不回落连接测试域",
        "不注册匿名 request_id 状态查询",
        "WebIntegrationStatusView",
        "不得复用 `WebConfigView`",
        "需使用本地管理员账号继续",
        "服务端不得仅凭 request ID 查询 `ActivationStore`",
        "B1 过渡期仍只向 `IdentitySource.LOCAL_ADMIN` 渲染并授权",
        "### W2-B1：登录、角色路由与四个独立 shell",
        "### W2-B2：配置 UI/API 迁入 Admin shell",
        "ProductRole.ADMIN + IdentitySource.LOCAL_ADMIN",
    ):
        assert required in plan, f"W2 计划仍留有实现期选择：{required}"
    assert "当前账号不能进入此页面" in spec
    assert "把 W2 已迁入 Admin shell 的当前 RI5 Gemini/飞书配置" in spec


# --- W4 计划批准与授权边界 ---------------------------------------------------
#
# 批准来源只能如实记录：没有公开审批 permalink 时写明"在 Codex 会话批准"，
# 合入事实不能冒充批准来源，也不能把 W4a 开工口令外推成 W4b 或真实调用许可。

_W4_PLAN = _ROOT / "docs/superpowers/plans/2026-09-24-w4-configuration-and-resource-registration.md"


def _w4_plan_header(text: str) -> str:
    return text[: text.find("## 1. 目标与交付方式")]


def _check_w4_plan_header(header: str) -> None:
    assert "Approved V0.1" in header
    assert "Review Draft" not in header
    assert "在 Codex 会话中批准" in header
    assert "没有公开的 GitHub 审批 permalink" in header
    assert "2cef6aa52e17eece8eb70ebce0057cfdddd8167a" in header
    assert "24e01d69f6c89367eb4d27bd7d93d990af5824dc" in header
    assert "合入事实本身不是批准来源" in header
    assert "Task 0–7（W4a）" in header
    assert "Task 8–11（W4b）" in header
    for gate in ("W4c", "W5", "真实 Provider", "真实 Secret", "联网", "部署", "canary", "UAT"):
        assert gate in header, f"W4 计划头部缺少未授权项：{gate}"
    assert "均未授权" in header


def test_w4_plan_header_records_the_approval_and_the_w4a_only_authority() -> None:
    _check_w4_plan_header(_w4_plan_header(_W4_PLAN.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Approved V0.1", "Review Draft V0.1"),
        ("没有公开的 GitHub 审批 permalink", "见审批链接"),
        ("合入事实本身不是批准来源", "合入即批准"),
        ("Task 8–11（W4b）", "Task 8–11"),
        ("均未授权", "已随本计划授权"),
    ],
)
def test_w4_plan_header_binding_is_discriminating(old: str, new: str) -> None:
    header = _w4_plan_header(_W4_PLAN.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError):
        _check_w4_plan_header(_replace_once(header, old, new))


# --- W4 收口、W5 计划、W5-A 合入与 W5-B 门 -----------------------------------
#
# W4a/W4b 已分别由 PR #81/#82 合入；W5 详细计划与 W5-A 已由 PR #83/#84 合入，负责人随后
# 口头授权 W5-B 离线实现。下一步只能审查 W5-B 候选，不能把计划、实现、部署、canary 或 UAT
# 写成同一件事。

_W4A_MERGE_COMMIT: Final[str] = "dcef09a903224fb86e8b053a15ab4865a7b731d1"
_W4B_MERGE_COMMIT: Final[str] = "88a63a054062db2041e51f4105846920040f3037"
_W5_PLAN_MERGE_COMMIT: Final[str] = "4e82b6ae4b3ea2ebb44bc60b2d74dd275a39a047"
_W5A_MERGE_COMMIT: Final[str] = "3c83ea5b087aa85b8ec06e097bc16526031df28a"
_W5A_REVIEWED_HEAD: Final[str] = "cfd0de47b7c0c5ce2ae8063ae06f10cdb073cbba"
_W5_PLAN = _ROOT / "docs/superpowers/plans/2026-09-24-w5-product-deployment.md"
_W5A_OVERCLAIMS: Final[tuple[str, ...]] = (
    "W5 已部署",
    "W5-B 已合入",
    "W5-A 已部署",
    "W5-B 已部署",
    "release 已部署",
    "RI6 已完成",
    "只读 V1 已发布",
    "canary 已通过",
)
_W4B_OVERCLAIMS: Final[tuple[str, ...]] = (
    "W4b 已部署",
    "W4b 已上线",
    "W4b 已验收",
    "资源已接入",
    "资源连接已验证",
    "已连接 StarRocks",
    "已连接 Prometheus",
)


def test_handoff_records_the_w5a_merge_and_points_only_to_w5b_review() -> None:
    handoff = _truth_doc_text("AGENT_HANDOFF.md")
    assert _current_baseline(handoff) == _W5A_MERGE_COMMIT
    assert _W4A_MERGE_COMMIT in handoff and "PR #81" in handoff
    assert _W4B_MERGE_COMMIT in handoff and "PR #82" in handoff
    assert _W5_PLAN_MERGE_COMMIT in handoff and "PR #83" in handoff
    assert _W5A_MERGE_COMMIT in handoff and "PR #84" in handoff
    assert _W5A_REVIEWED_HEAD in handoff
    assert "W4a/W4b 已离线实现并合入" in handoff
    assert "W5 详细计划 Approved V0.2" in handoff
    assert "W5-A 已离线实现并合入" in handoff
    assert "W5-B 尚未合入" in handoff
    assert "W5-B（Task 6–9）" in handoff
    assert "无公开 GitHub permalink" in handoff
    for overclaim in (*_W4B_OVERCLAIMS, *_W5A_OVERCLAIMS):
        assert overclaim not in handoff, overclaim
    stage = _handoff_baseline_field(handoff, "阶段")
    assert "W5-A" in stage and "PR #84" in stage
    assert "W5-B" in stage and "尚未合入" in stage
    next_step = _handoff_baseline_field(handoff, "下一步")
    assert "W5-B" in next_step and "exact-SHA" in next_step
    assert "W5-C" in next_step and "GO" in next_step
    for forbidden_claim in ("开始部署", "开始 canary", "开始 UAT"):
        assert forbidden_claim not in next_step


def test_w5_plan_keeps_release_safety_and_evidence_gates_separate() -> None:
    assert _W5_PLAN.is_file(), "W5 详细计划尚未落盘"
    plan = _W5_PLAN.read_text(encoding="utf-8")
    header = "\n".join(plan.splitlines()[:24])
    assert header.startswith("# W5 产品发布与分级验收实施计划")
    assert "状态：Approved V0.2" in header
    assert "PR #83" in header and "W5-A（Task 0–5）" in header
    assert _W4B_MERGE_COMMIT in header
    assert "计划送审不授权" in header
    for gate in ("源码", "部署", "真实调用", "canary", "UAT"):
        assert gate in header
    assert "2026-09-10-compose-deployment-canary-uat.md" in plan
    assert "历史输入" in plan and "不再作为可执行计划" in plan
    assert "30 天" in plan and "终态激活" in plan
    assert "offline_recording" in plan and "release" in plan
    assert "fake/recording" in plan and "release" in plan
    assert "docker-compose.release.yml" in plan
    for route in (
        "/login/api/login",
        "/oauth/feishu/start",
        "/oauth/feishu/callback",
        "/admin/api/activations/approve",
        "/admin/api/activations/reject",
    ):
        assert route in plan
    for independent_gate in ("W4c", "H 层", "E1", "RI2", "RI3", "RI4", "RI6"):
        assert independent_gate in plan
    for evidence_level in ("deployed SHA", "canary", "user-accepted"):
        assert evidence_level in plan


def test_w5_plan_closes_release_snapshot_history_and_scope_gaps() -> None:
    plan = _W5_PLAN.read_text(encoding="utf-8")

    release_decisions = _section_between(
        plan,
        start="### 2.2 release 不允许合成执行，也不虚报当前能力",
        end="### 2.3 旧身份文件只允许一次性挂载",
    )
    task_1 = _section_between(
        plan,
        start="### Task 1：实现 release runtime profile",
        end="### Task 2：实现固定终态保留与维护 CLI",
    )
    task_3 = _section_between(
        plan,
        start="### Task 3：旧身份迁移 CLI 与运行时去旧真源",
        end="### Task 4：增加发布预检，不把运行中任务跨 profile 接管",
    )
    task_4 = _section_between(
        plan,
        start="### Task 4：增加发布预检，不把运行中任务跨 profile 接管",
        end="### Task 5：W5-A 集成、自审与合并门",
    )
    w5_c = _section_between(
        plan,
        start="## 6. W5-C 任务：获准环境执行",
        end="## 7. 独立门与明确非目标",
    )

    # 当前普通对话与历史计划投影是两份不同事实；所有承载 TaskViewRuntime
    # 的进程都必须消费 release 准入快照，不能只修 worker 内嵌的 view。
    for fact in (
        "conversation_snapshot",
        "rendering_bindings",
        "XIAOWEI_RUNTIME_PROFILE=release",
        "_FAKE_MODULES",
    ):
        assert fact in release_decisions or fact in task_1
    for process in (
        "internal-api",
        "web-app",
        "feishu-listener",
        "channel-worker",
        "worker",
    ):
        assert process in release_decisions and process in task_1
    for path in (
        "src/xiaowei_agent/application/task_view_runtime.py",
        "src/xiaowei_agent/application/runtime.py",
        "tests/security/test_task_view_runtime_authority.py",
    ):
        assert path in task_1

    # 首次 release 不能继承无可信运行来源的历史计划/证据；状态判断直接复用
    # 契约真源，不能再手写一份终态/非终态清单。
    assert "historical_execution_data_present" in task_4
    assert "TERMINAL_STATUSES" in task_4
    assert "SUCCEEDED" in task_4 and "plan/evidence" in task_4
    assert "新数据库" in task_4 and "单独批准的数据处置" in task_4

    # W5 V1 是固定作用域、provider-off 的产品壳。它不把 test_readonly
    # 或 recording 偷渡进 release，也不冒充 RI6 的正式发布证据。
    assert "starrocks_adapter_mode=disabled" in release_decisions
    assert "dev-local/dev" in release_decisions
    assert "provider-off 产品壳" in plan
    assert "不构成 RI6" in plan and "RI6" in w5_c

    # 维护命令不外泄旧身份或受控 PII；首次发布没有安全旧 release 时只能停服。
    assert "全库所有作用域" in plan
    for count in ("created_count", "skipped_count", "deferred_count"):
        assert count in task_3
    for omitted_file in (
        ".env.example",
        "README.md",
        "docker-compose.feishu.yml",
        "docker-compose.smoke.yml",
        "scripts/compose_smoke.py",
        "tests/contract/test_compose_contract.py",
        "tests/contract/test_compose_smoke_script.py",
        "tests/security/test_ri5_compose_boundary.py",
    ):
        assert omitted_file in task_3
    assert "首次部署" in w5_c and "停止全部应用服务" in w5_c
    assert "recording" in w5_c


def test_w5a_docs_match_the_implemented_release_constants() -> None:
    """ARCHITECTURE/README 描述的 W5-A 事实必须与代码常量逐字一致，不留第二个真源。"""
    from xiaowei_agent.capabilities.registry import PROVIDER_OFF_SNAPSHOT_ID
    from xiaowei_agent.interfaces.legacy_identity_migration import (
        LEGACY_IDENTITY_DOCUMENT_PATH,
    )
    from xiaowei_agent.persistence.activation import ACTIVATION_TERMINAL_RETENTION_DAYS

    architecture = _truth_doc_text("ARCHITECTURE.md")
    readme = _truth_doc_text("README.md")
    for token in (
        "conversation_snapshot",
        "rendering_bindings",
        "offline_recording",
        PROVIDER_OFF_SNAPSHOT_ID,
        LEGACY_IDENTITY_DOCUMENT_PATH,
        f"{ACTIVATION_TERMINAL_RETENTION_DAYS} 天",
        "全库所有作用域",
        "tasks_not_drained",
        "historical_execution_data_present",
    ):
        assert token in architecture, token
    for command in (
        "xiaowei_agent.interfaces.legacy_identity_migration",
        "xiaowei_agent.interfaces.activation_retention",
        "xiaowei_agent.interfaces.release_preflight",
    ):
        assert command in readme, command
    assert LEGACY_IDENTITY_DOCUMENT_PATH in readme
    assert "docker-compose.feishu.yml" not in readme
    assert "XIAOWEI_FEISHU_IDENTITY_FILE" not in readme


def test_env_example_rollback_never_returns_release_to_recording() -> None:
    """运维入口的回滚说明必须与 W5 计划一致：只回退到 W5-compatible release digest。

    首次部署没有安全旧 digest 时停服并保留数据库与证据；任何写法都不得把
    offline_recording 当作 release 的恢复目标，否则发布库会重新写入合成执行事实。
    """
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    profile_block = env_example.split("XIAOWEI_RUNTIME_PROFILE=", 1)[0].rsplit("\n\n", 1)[1]

    assert "不得回退到 offline_recording" in profile_block
    assert "W5-compatible release" in profile_block and "不可变 digest" in profile_block
    assert "首次部署" in profile_block and "停止全部应用服务" in profile_block
    assert "保留数据库与证据" in profile_block
    for forbidden in ("改回 offline_recording", "回滚即改回", "切回 offline_recording"):
        assert forbidden not in env_example, forbidden


def test_w5b_docs_match_the_release_compose_contract() -> None:
    """README/ARCHITECTURE 描述的 W5-B 事实必须与检查器常量一致，不留第二个真源。"""
    from scripts.release_compose import RELEASE_COMPOSE_FILES, RELEASE_TEMPLATE_KEYS

    architecture = _truth_doc_text("ARCHITECTURE.md")
    readme = _truth_doc_text("README.md")
    file_set = " + ".join(f"`{name}`" for name in RELEASE_COMPOSE_FILES)
    for text in (architecture, readme):
        assert file_set in text
        assert "name@sha256:<64 hex>" in text
        assert "字面量" in text
        assert "docs/runbooks/w5-product-deployment.md" in text
        for overclaim in _W5A_OVERCLAIMS:
            assert overclaim not in text, overclaim
    assert "scripts.release_compose" in architecture
    assert "python -m scripts.release_compose --env-file" in readme
    assert "release-compose: ok" in readme
    assert "docs/examples/w5-release.env.example" in readme
    assert len(RELEASE_TEMPLATE_KEYS) == 6 and "只有六个键" in readme
    assert "不进 wheel/镜像" in architecture
    assert "`not_applicable`" in architecture
    assert "**绝不**回到 recording 栈" in readme
    assert "provider-off release smoke" in readme


def test_w5_plan_task_2_keeps_the_global_retention_lock() -> None:
    """复审非阻断建议：Task 2 的全局锁与全库语义按章节绑定，删句会转红。"""
    task_2 = _section_between(
        _W5_PLAN.read_text(encoding="utf-8"),
        start="### Task 2：实现固定终态保留与维护 CLI",
        end="### Task 3：旧身份迁移 CLI 与运行时去旧真源",
    )
    assert "全局 activation advisory lock" in task_2
    assert "全库所有作用域" in task_2
    assert "不接受 scope 参数" in task_2


def test_readme_and_architecture_describe_resources_as_registration_only() -> None:
    readme = _truth_doc_text("README.md")
    architecture = _truth_doc_text("ARCHITECTURE.md")
    assert "`.config/resources/config.json`" in readme
    assert "已保存，尚未接入" in readme
    for text in (readme, architecture):
        assert "StarRocks" in text and "Prometheus" in text
        assert "不解析 DNS" in text
        for overclaim in _W4B_OVERCLAIMS:
            assert overclaim not in text, overclaim
    assert "(worker, resources)" in architecture

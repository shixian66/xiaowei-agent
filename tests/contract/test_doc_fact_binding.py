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

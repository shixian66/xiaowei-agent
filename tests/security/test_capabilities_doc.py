"""docs/CAPABILITIES.md 必须与 Registry 快照一致。

手工维护的能力总表会与声明漂移，而漂移方向通常是"文档说有、代码没有"——那正是
把一个未实现的能力当成已有能力的起点。
"""

from pathlib import Path

import pytest

from xiaowei_agent.capabilities.doc import render_capabilities_doc
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry

pytestmark = pytest.mark.security

_DOC = Path(__file__).resolve().parents[2] / "docs" / "CAPABILITIES.md"


def test_capabilities_doc_matches_the_registry() -> None:
    assert _DOC.read_text(encoding="utf-8") == render_capabilities_doc(
        StaticCapabilityRegistry().snapshot()
    )


def test_capabilities_doc_does_not_list_the_synthetic_write_capability() -> None:
    """合成写能力只存在于测试夹具，不得出现在能力地图里。"""
    assert "test.synthetic.write" not in _DOC.read_text(encoding="utf-8")


def test_capabilities_doc_does_not_claim_deployment() -> None:
    """能力状态最强为 `tests`：不得**声称**已部署、canary 或用户验收。

    断言的是**肯定式声称**而不是关键词本身：文件里出现"未 canary"是正确的表述，
    按关键词禁用反而会逼着作者把这句限定删掉——那才是真正的退步。
    """
    text = _DOC.read_text(encoding="utf-8")
    for claim in ("已部署", "已上线", "已 canary", "已用户验收", "deployed SHA"):
        assert claim not in text
    assert "未部署" in text
    assert "`tests`" in text

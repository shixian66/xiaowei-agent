"""trace / audit 事件的 detail 只放已脱敏短标签，不放原始 payload。

复用下沉后的 redaction 模块，避免第二套会漂移的脱敏规则。
"""

import ast
import datetime as dt
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import PipelineStage, StageOutcome, TraceEvent

# 伪造取值必须**拆开写**：连续的 ``token=<值>`` 会被 secret-scan 判为泄漏。
# 这是仓库既有约定（见 tests/security/test_redaction.py 的 _PW / _TOKEN），
# 由 test_no_contiguous_secret_shaped_literal 在本地 gate 内强制。
_FAKE = "abc123" + "def456"

pytestmark = pytest.mark.security


def _event(detail: dict[str, str]) -> TraceEvent:
    return TraceEvent(
        event_id="e1",
        trace_id="0" * 32,
        task_id="t1",
        stage=PipelineStage.GATEWAY,
        outcome=StageOutcome.OK,
        occurred_at=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
        capability_id=None,
        step_id="s1",
        policy_revision="policy-2026-09-01",
        error=None,
        detail=detail,
    )


def test_detail_values_are_redacted_on_construction() -> None:
    event = _event({"upstream": "password=hunter2 rows=3"})
    assert "hunter2" not in event.detail["upstream"]
    assert "***" in event.detail["upstream"]


def test_detail_rejects_oversized_values() -> None:
    """detail 是标签不是载荷；超长即拒绝，防止有人把整个响应塞进 trace。"""
    with pytest.raises(ValidationError):
        _event({"payload": "x" * 1024})


def test_detail_is_deeply_immutable() -> None:
    event = _event({"k": "v"})
    with pytest.raises(TypeError):
        event.detail["k"] = "tampered"  # type: ignore[index]


def test_redaction_survives_a_copy() -> None:
    """copy 会重新走完整校验，脱敏与冻结都必须再次生效。"""
    event = _event({"k": "v"})
    copied = event.model_copy(update={"detail": {"k": "token=" + _FAKE}})
    assert _FAKE not in copied.detail["k"]
    with pytest.raises(TypeError):
        copied.detail["k"] = "x"  # type: ignore[index]


def test_stage_enum_covers_the_ten_architecture_stages() -> None:
    assert {s.value for s in PipelineStage} == {
        "intent",
        "model",
        "resolver",
        "planner",
        "admission",
        "gateway",
        "evidence",
        "reflection",
        "rendering",
        "lifecycle",
    }


def test_trace_event_does_not_import_the_logging_module() -> None:
    """contracts 必须依赖下沉后的 redaction，而不是 log。"""
    from xiaowei_agent.contracts import trace_events

    source = Path(inspect.getfile(trace_events)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        assert (module or "") != "xiaowei_agent.log"

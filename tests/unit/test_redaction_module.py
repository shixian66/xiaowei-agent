"""脱敏规则只有一份实现，且位于分层最底层。"""

import ast
from pathlib import Path

from xiaowei_agent import log, redaction


def test_log_reexports_the_same_objects() -> None:
    """log 必须复用同一份实现，而不是另写一套会漂移的规则。"""
    assert log.scrub_text is redaction.scrub_text
    assert log.redact is redaction.redact
    assert log.REDACTED is redaction.REDACTED
    assert log.JsonValue is redaction.JsonValue


def test_redaction_module_has_no_internal_dependency() -> None:
    """它是叶子：不 import 任何 xiaowei_agent 模块。

    否则 contracts 复用它就会重新引入对内依赖，§2 取向 3 的分层失效。
    """
    source = Path(redaction.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        assert not (module or "").startswith("xiaowei_agent"), module


def test_redaction_module_does_not_import_logging() -> None:
    """logging 关注点必须留在 log.py，否则下沉没有真正解耦。"""
    source = Path(redaction.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        assert (module or "").split(".")[0] != "logging"

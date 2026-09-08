"""完整 TaskSubmission 的读取调用点是显式闭集。"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_ALLOWED = {
    Path("application/channel_access.py"),
    Path("application/channel_projection.py"),
}


def _submission_read_callers() -> set[Path]:
    callers: set[Path] = set()
    for path in _PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_submission"
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(_PACKAGE))
    return callers


def test_task_submission_read_callers_are_the_approved_closed_set() -> None:
    assert _submission_read_callers() == _ALLOWED

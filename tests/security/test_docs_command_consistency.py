"""ADR-008 的四条验证命令在全部 Markdown 中必须逐字一致。"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = (
    "python -m pytest -q",
    "python -m pytest -m security -q",
    "ruff check .",
    "mypy src",
)
_BARE = ("\npytest -q", "\npytest -m security -q", "`pytest -q`", "`pytest -m security -q`")


def _docs() -> list[Path]:
    return [p for p in _ROOT.rglob("*.md") if ".venv" not in p.parts and ".git" not in p.parts]


def test_no_bare_pytest_invocation() -> None:
    for path in _docs():
        text = path.read_text(encoding="utf-8")
        for bad in _BARE:
            assert bad not in text, f"{path.relative_to(_ROOT)} 含非规范命令 {bad!r}"


def test_canonical_commands_present_in_adr_008() -> None:
    adr = next(_ROOT.glob("docs/adr/ADR-008-*.md")).read_text(encoding="utf-8")
    for cmd in _CANONICAL:
        assert cmd in adr, f"ADR-008 缺少规范命令 {cmd!r}"

""".env.example 不得含任何真实凭证形状的取值。"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SECRET_SHAPES = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+:[^\s/@]+@"
    r"|\b(?:sk-|ghp_|gho_|AKIA)[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _pairs() -> list[tuple[str, str]]:
    out = []
    for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out.append((k.strip(), v.strip()))
    return out


def test_file_exists_and_has_entries() -> None:
    assert _pairs()


def test_no_secret_shaped_values() -> None:
    for key, value in _pairs():
        assert not _SECRET_SHAPES.search(value), f"{key} 的取值形似真实凭证"


def test_documents_only_known_variables() -> None:
    from xiaowei_agent.config import _FIELD_TO_ENV

    assert {k for k, _ in _pairs()} <= set(_FIELD_TO_ENV.values())

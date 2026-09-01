"""CI workflow 的安全策略约束。

用标准库读文本 + 精确断言，不引入 YAML parser。
gitleaks 只能发现凭证字面量，无法证明这些配置约束，故单独承重。
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_TEXT = _WF.read_text(encoding="utf-8")

_EXPECTED_JOBS = ("tests", "security-gate", "lint", "types", "deps-audit", "secret-scan")


def test_workflow_exists() -> None:
    assert _WF.is_file()


def test_no_pull_request_target() -> None:
    assert "pull_request_target" not in _TEXT


def test_no_secrets_context() -> None:
    assert not re.search(r"\$\{\{\s*secrets\.", _TEXT)


def test_workflow_level_permissions_are_read_only() -> None:
    assert re.search(r"(?m)^permissions:\n  contents: read\n", _TEXT)
    assert "write" not in re.findall(r"(?m)^permissions:\n(?:  .*\n)+", _TEXT)[0]


def test_every_checkout_disables_credential_persistence() -> None:
    checkouts = _TEXT.count("uses: actions/checkout@")
    persist = _TEXT.count("persist-credentials: false")
    assert checkouts == len(_EXPECTED_JOBS)
    assert persist == checkouts


def test_all_actions_pinned_to_full_commit_sha() -> None:
    refs = re.findall(r"uses:\s*([^\s@]+)@(\S+)", _TEXT)
    assert refs
    for name, ref in refs:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{name} 未钉到 40 位 commit SHA: {ref}"


def test_runner_is_pinned_not_latest() -> None:
    assert "ubuntu-latest" not in _TEXT
    assert _TEXT.count("runs-on: ubuntu-24.04") == len(_EXPECTED_JOBS)


def test_all_six_gates_present_with_stable_names() -> None:
    for job in _EXPECTED_JOBS:
        assert re.search(rf"(?m)^    name: {re.escape(job)}$", _TEXT), f"缺少 check 名 {job}"


def test_gates_run_adr008_commands_verbatim() -> None:
    for cmd in ("python -m pytest -q", "python -m pytest -m security -q",
                "ruff check .", "mypy src"):
        assert f"- run: {cmd}\n" in _TEXT, f"未原样执行 ADR-008 命令: {cmd}"


def test_secret_scan_uses_full_history_and_pinned_checksum() -> None:
    assert "fetch-depth: 0" in _TEXT
    assert re.search(r'GITLEAKS_SHA256:\s*"[0-9a-f]{64}"', _TEXT)
    assert "sha256sum -c -" in _TEXT


def test_gates_are_not_piped() -> None:
    for line in _TEXT.splitlines():
        if line.strip().startswith("- run:"):
            assert "|" not in line, f"gate 命令不得接管道，退出码会被吞: {line.strip()}"

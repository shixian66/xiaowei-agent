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


def test_no_secrets_context_in_any_syntax() -> None:
    """点号与方括号两种引用形式都必须拒绝。"""
    assert not re.search(r"\$\{\{[^}]*\bsecrets\s*[.\[]", _TEXT), "禁止引用 secrets context"


def test_no_github_token_reference_in_any_syntax() -> None:
    """GitHub 的属性访问同时支持点号与方括号索引，两种都必须拒绝。"""
    assert not re.search(r"\$\{\{[^}]*\bgithub\s*\.\s*token\b", _TEXT)
    assert not re.search(r"\$\{\{[^}]*\bgithub\s*\[\s*['\"]token", _TEXT)
    assert not re.search(r"\$\{\{[^}]*\bsecrets\s*[.\[]\s*['\"]?GITHUB_TOKEN", _TEXT)


def test_no_secrets_key_anywhere_including_inherit() -> None:
    """`secrets: inherit` 会把全部 secrets 传给被复用 workflow，必须整体禁止。"""
    hits = re.findall(r"(?m)^[ \t]*secrets\s*:", _TEXT)
    assert hits == [], f"禁止 workflow/job 级 secrets 键，实际命中 {hits}"


def _job_ids() -> list[str]:
    """不引入 YAML parser：从 `jobs:` 块中按缩进提取 job id。"""
    lines = _TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    ids: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(" "):
            break
        match = re.fullmatch(r"  ([A-Za-z_][\w-]*):", line.rstrip())
        if match:
            ids.append(match.group(1))
    return ids


def test_job_set_is_exactly_the_approved_six() -> None:
    """只检查 6 个 gate『存在』不够——多出的 job 同样会引入未审查的执行面。"""
    assert sorted(_job_ids()) == sorted(_EXPECTED_JOBS), f"实际 job 集合 = {_job_ids()}"


def test_exactly_one_permissions_block_and_it_is_workflow_level() -> None:
    """job 级 permissions 覆盖会绕过顶层最小权限，必须整体禁止。"""
    blocks = re.findall(r"(?m)^([ \t]*)permissions:", _TEXT)
    assert blocks == [""], f"只允许一个顶层 permissions 块，实际缩进集合={blocks}"


def test_workflow_permissions_are_read_only() -> None:
    block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", _TEXT)
    assert block, "缺少顶层 permissions 块"
    assert block.group(1).strip() == "contents: read"


def test_no_write_or_write_all_permission_anywhere() -> None:
    assert "write-all" not in _TEXT
    assert not re.search(r"(?m)^\s*permissions:\s*write-all\s*$", _TEXT)
    assert not re.search(r"(?m)^\s+\w[\w-]*:\s*write\s*$", _TEXT)


def test_no_env_block_outside_declared_allowlist() -> None:
    """workflow/job 级 env 只允许 secret-scan 的 gitleaks 钉版变量。"""
    allowed = {"GITLEAKS_VERSION", "GITLEAKS_SHA256"}
    names = set(re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]*):\s", _TEXT))
    assert names <= allowed, f"出现未声明的 env 变量: {sorted(names - allowed)}"


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

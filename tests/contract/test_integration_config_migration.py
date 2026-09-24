"""旧 ``integrations.json`` → AI/飞书两域的一次性、可重跑迁移。

矩阵覆盖计划 Task 2 的每一格：空目录、legacy-only、split-only、部分写后重跑、两份新文件
已精确匹配、任一新文件冲突、旧文件损坏、目标符号链接、第二份写失败、重读失败、
unlink/fsync 失败。**所有失败分支都断言旧文件仍在且已有新文件未被覆盖**；只有成功
分支才断言旧文件消失。
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from xiaowei_agent.contracts import (
    AiConfig,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
)
from xiaowei_agent.interfaces import integration_config_migrate
from xiaowei_agent.interfaces.integration_config_file import (
    IntegrationConfigError,
    read_ai_config,
    read_feishu_config,
    write_ai_config,
    write_feishu_config,
)
from xiaowei_agent.interfaces.integration_config_migrate import (
    MigrationResult,
    migrate_legacy_integration_config,
)

_FAKE_KEY: Final = "AIza" + "-migration-not-real"
_FAKE_SECRET: Final = "app" + "-migration-not-real"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    root.mkdir(mode=0o700)
    for domain in ("ai", "feishu", "resources"):
        (root / domain).mkdir(mode=0o700)
    return root


def _legacy(root: Path, *, generation: int = 4) -> Path:
    legacy = root / "integrations.json"
    legacy.write_text(
        json.dumps(
            {
                "generation": generation,
                "gemini": {"enabled": True, "api_key": _FAKE_KEY},
                "feishu": {"enabled": True, "app_id": "cli_m", "app_secret": _FAKE_SECRET},
            }
        ),
        encoding="utf-8",
    )
    legacy.chmod(0o600)
    return legacy


def _expected_ai(generation: int = 4) -> AiConfig:
    return AiConfig(
        generation=generation, gemini=GeminiIntegration(enabled=True, api_key=_FAKE_KEY)
    )


def _expected_feishu(generation: int = 4) -> FeishuConfig:
    return FeishuConfig(
        generation=generation,
        feishu=FeishuIntegration(enabled=True, app_id="cli_m", app_secret=_FAKE_SECRET),
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _migrate(root: Path) -> MigrationResult:
    return migrate_legacy_integration_config(config_root=str(root))


# --------------------------------------------------------------------------
# 成功与无事可做
# --------------------------------------------------------------------------


def test_empty_directory_has_nothing_to_migrate(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert _migrate(root) is MigrationResult.NOTHING_TO_MIGRATE
    assert _snapshot(root) == {}


def test_legacy_only_is_split_into_two_same_generation_domains(tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root, generation=4)
    assert _migrate(root) is MigrationResult.MIGRATED
    assert not legacy.exists()
    assert read_ai_config(str(root / "ai" / "config.json")) == _expected_ai(4)
    assert read_feishu_config(str(root / "feishu" / "config.json")) == _expected_feishu(4)
    # resources 域只有目录：迁移不为它凭空造文件。
    assert list((root / "resources").iterdir()) == []


def test_split_only_is_already_migrated(tmp_path: Path) -> None:
    root = _root(tmp_path)
    write_ai_config(str(root / "ai" / "config.json"), _expected_ai(2))
    write_feishu_config(str(root / "feishu" / "config.json"), _expected_feishu(5))
    before = _snapshot(root)
    assert _migrate(root) is MigrationResult.ALREADY_MIGRATED
    assert _snapshot(root) == before


def test_rerun_after_success_is_idempotent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _legacy(root)
    assert _migrate(root) is MigrationResult.MIGRATED
    after_first = _snapshot(root)
    assert _migrate(root) is MigrationResult.ALREADY_MIGRATED
    assert _snapshot(root) == after_first


def test_both_new_files_already_exactly_matching_only_removes_the_legacy(
    tmp_path: Path,
) -> None:
    """崩溃点：两份都写完，删旧文件之前进程死了。重跑只删旧文件。"""
    root = _root(tmp_path)
    legacy = _legacy(root)
    write_ai_config(str(root / "ai" / "config.json"), _expected_ai())
    write_feishu_config(str(root / "feishu" / "config.json"), _expected_feishu())
    ai_inode = (root / "ai" / "config.json").stat().st_ino
    assert _migrate(root) is MigrationResult.MIGRATED
    assert not legacy.exists()
    assert (root / "ai" / "config.json").stat().st_ino == ai_inode


def test_partial_write_then_rerun_completes_the_missing_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩溃点：AI 写完、飞书没写。重跑接受相等的 AI，只补飞书。"""
    root = _root(tmp_path)
    legacy = _legacy(root)

    def _crash(*_: object, **__: object) -> None:
        raise IntegrationConfigError("integration config unavailable")

    monkeypatch.setattr(integration_config_migrate, "write_feishu_config", _crash)
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert legacy.exists()
    assert (root / "ai" / "config.json").exists()
    assert not (root / "feishu" / "config.json").exists()
    ai_inode = (root / "ai" / "config.json").stat().st_ino

    monkeypatch.undo()
    assert _migrate(root) is MigrationResult.MIGRATED
    assert not legacy.exists()
    assert (root / "ai" / "config.json").stat().st_ino == ai_inode
    assert read_feishu_config(str(root / "feishu" / "config.json")) == _expected_feishu()


# --------------------------------------------------------------------------
# 冲突与损坏：绝不覆盖任何一边
# --------------------------------------------------------------------------


def _conflicting_ai() -> AiConfig:
    return AiConfig(generation=4, gemini=GeminiIntegration(enabled=True, api_key="k" * 16))


def _conflicting_feishu() -> FeishuConfig:
    return FeishuConfig(
        generation=4,
        feishu=FeishuIntegration(enabled=True, app_id="cli_other", app_secret=_FAKE_SECRET),
    )


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(lambda root: write_ai_config(
            str(root / "ai" / "config.json"), _conflicting_ai()
        ), id="ai-secret-differs"),
        pytest.param(lambda root: write_ai_config(
            str(root / "ai" / "config.json"), _expected_ai(5)
        ), id="ai-generation-differs"),
        pytest.param(lambda root: write_feishu_config(
            str(root / "feishu" / "config.json"), _conflicting_feishu()
        ), id="feishu-app-id-differs"),
        pytest.param(lambda root: (root / "feishu" / "config.json").write_text(
            "{not json", encoding="utf-8"
        ), id="feishu-corrupt"),
    ],
)
def test_any_existing_new_file_that_does_not_exactly_match_is_migration_required(
    tmp_path: Path, existing: Callable[[Path], object]
) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)
    existing(root)
    before = _snapshot(root)
    assert _migrate(root) is MigrationResult.MIGRATION_REQUIRED
    assert legacy.exists()
    assert _snapshot(root) == before


def test_corrupt_legacy_is_migration_required_and_touches_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy = root / "integrations.json"
    legacy.write_text('{"generation": 1, "gemini": {"enabled": false}}', encoding="utf-8")
    before = _snapshot(root)
    assert _migrate(root) is MigrationResult.MIGRATION_REQUIRED
    assert _snapshot(root) == before


def test_symlinked_target_file_is_never_followed_or_replaced(tmp_path: Path) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    (root / "ai" / "config.json").symlink_to(outside)
    assert _migrate(root) is MigrationResult.MIGRATION_REQUIRED
    assert legacy.exists()
    assert outside.read_text(encoding="utf-8") == "keep"
    assert (root / "ai" / "config.json").is_symlink()
    assert not (root / "feishu" / "config.json").exists()


def test_symlinked_legacy_file_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _legacy(root).rename(tmp_path / "elsewhere.json")
    (root / "integrations.json").symlink_to(tmp_path / "elsewhere.json")
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert (tmp_path / "elsewhere.json").exists()
    assert not (root / "ai" / "config.json").exists()


@pytest.mark.parametrize("domain", ["ai", "feishu", "resources"])
def test_symlinked_or_missing_domain_directory_is_unavailable(
    tmp_path: Path, domain: str
) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)
    (root / domain).rmdir()
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    other = tmp_path / "other"
    other.mkdir()
    (root / domain).symlink_to(other)
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert legacy.exists()
    assert list(other.iterdir()) == []


@pytest.mark.parametrize("root_kind", ["relative", "missing", "symlink", "file"])
def test_config_root_must_be_an_absolute_real_directory(
    tmp_path: Path, root_kind: str
) -> None:
    real = _root(tmp_path)
    _legacy(real)
    if root_kind == "relative":
        target = "config"
    elif root_kind == "missing":
        target = str(tmp_path / "absent")
    elif root_kind == "symlink":
        (tmp_path / "link").symlink_to(real)
        target = str(tmp_path / "link")
    else:
        (tmp_path / "plain").write_text("x", encoding="utf-8")
        target = str(tmp_path / "plain")
    assert migrate_legacy_integration_config(config_root=target) is MigrationResult.UNAVAILABLE
    assert (real / "integrations.json").exists()


# --------------------------------------------------------------------------
# I/O 失败：保留旧文件，已写的一域也不被回滚或覆盖
# --------------------------------------------------------------------------


def test_second_write_failure_keeps_the_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)
    monkeypatch.setattr(
        integration_config_migrate,
        "write_feishu_config",
        lambda *_a, **_k: (_ for _ in ()).throw(
            IntegrationConfigError("integration config unavailable")
        ),
    )
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert legacy.exists()
    assert read_ai_config(str(root / "ai" / "config.json")) == _expected_ai()


def test_readback_mismatch_after_write_keeps_the_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)
    monkeypatch.setattr(
        integration_config_migrate,
        "read_feishu_config",
        lambda *_a, **_k: _conflicting_feishu(),
    )
    assert _migrate(root) is not MigrationResult.MIGRATED
    assert legacy.exists()


def test_readback_failure_after_write_keeps_the_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)
    real_read = integration_config_migrate.read_ai_config
    calls: list[str] = []

    def _fails_after_write(path: str) -> AiConfig:
        calls.append(path)
        if len(calls) >= 2:
            raise IntegrationConfigError("integration config unavailable")
        return real_read(path)

    monkeypatch.setattr(integration_config_migrate, "read_ai_config", _fails_after_write)
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert legacy.exists()


def test_unlink_failure_keeps_everything_and_is_rerunnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    legacy = _legacy(root)

    def _refuse(*_: object, **__: object) -> None:
        raise OSError("unlink refused")

    monkeypatch.setattr(integration_config_migrate.os, "unlink", _refuse)
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert legacy.exists()
    monkeypatch.undo()
    assert _migrate(root) is MigrationResult.MIGRATED
    assert not legacy.exists()


def test_directory_fsync_failure_after_unlink_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧文件删掉之后目录 fsync 失败：删除的持久性未知，不能报 MIGRATED。"""
    root = _root(tmp_path)
    _legacy(root)
    real_unlink = os.unlink
    real_fsync = os.fsync
    unlinked: list[object] = []

    def _record_unlink(*args: object, **kwargs: object) -> None:
        unlinked.append(args[0])
        real_unlink(*args, **kwargs)  # type: ignore[arg-type]

    def _refuse_after_unlink(descriptor: int) -> None:
        if unlinked:
            raise OSError("fsync refused")
        real_fsync(descriptor)

    monkeypatch.setattr(integration_config_migrate.os, "unlink", _record_unlink)
    monkeypatch.setattr(integration_config_migrate.os, "fsync", _refuse_after_unlink)
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    monkeypatch.undo()
    # 重跑：旧文件已不在、两域完整——结论收敛为 ALREADY_MIGRATED。
    assert _migrate(root) is MigrationResult.ALREADY_MIGRATED


def test_legacy_replaced_between_read_and_unlink_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读到的旧文件与准备删除的必须是同一个 inode；被替换时不删。"""
    root = _root(tmp_path)
    legacy = _legacy(root)
    real_write = integration_config_migrate.write_feishu_config

    def _swap_legacy(path: str, config: FeishuConfig) -> None:
        real_write(path, config)
        replacement = root / "replacement.json"
        replacement.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(replacement, legacy)

    monkeypatch.setattr(integration_config_migrate, "write_feishu_config", _swap_legacy)
    assert _migrate(root) is MigrationResult.UNAVAILABLE
    assert legacy.exists()


def test_no_secret_or_path_reaches_the_result_or_output(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _legacy(root)
    result = _migrate(root)
    for rendered in (repr(result), str(result), result.value):
        assert _FAKE_KEY not in rendered
        assert _FAKE_SECRET not in rendered
        assert str(root) not in rendered


def test_cli_prints_only_a_closed_result_code() -> None:
    """CLI 不接收任何参数：多一个参数就是多一条"迁移了另一个目录"的路。"""
    completed = subprocess.run(  # noqa: S603 -- 固定解释器与模块
        [sys.executable, "-m", "xiaowei_agent.interfaces.integration_config_migrate", "/tmp"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout.strip() == "migration: usage"
    assert completed.stderr == ""


def test_main_maps_results_to_closed_lines_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _legacy(root)
    monkeypatch.setattr(integration_config_migrate, "CONFIG_ROOT", str(root))
    assert integration_config_migrate.main([]) == 0
    assert capsys.readouterr().out == "migration: migrated\n"
    assert integration_config_migrate.main([]) == 0
    assert capsys.readouterr().out == "migration: already_migrated\n"
    (root / "ai" / "config.json").write_text("{bad", encoding="utf-8")
    _legacy(root)
    assert integration_config_migrate.main([]) == 1
    assert capsys.readouterr().out == "migration: migration_required\n"

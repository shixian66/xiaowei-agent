"""旧 ``integrations.json`` 到 AI/飞书两域的**显式、一次性、可重跑**迁移。

这不是任何服务启动路径的一部分：没有 import 它的服务入口（安全测试钉住），也不在
运行时双读、回落或自动迁移。运维先停止消费者，再在宿主上显式执行
``python -m xiaowei_agent.interfaces.integration_config_migrate``。

算法固定为：

1. 以 dir-fd + ``O_NOFOLLOW`` 验证绝对 ``config_root``、旧文件与三个子目录；
2. 没有旧文件：新两域都不存在是 ``nothing_to_migrate``，都有效是 ``already_migrated``；
3. 有旧文件时严格读取，映射成同 generation 的 :class:`AiConfig` 与 :class:`FeishuConfig`；
4. **先检查、后写入**：任一已存在的新文件必须与映射结果逐字段完全相等（含 Secret），
   不等或损坏即 ``migration_required``，不覆盖任何一边；
5. 缺失的一域用正常原子 writer 写入，每写一份立即重读比对；
6. 两域都重读相等、且旧文件仍是当初读到的那个 inode，才删除旧文件并 fsync 父目录。

任一步崩溃后都可以重跑：已写且相等的一域被接受，缺失的一域继续写。结果只有闭集码，
不携带路径、内容或异常正文。
"""

import os
import stat
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Final, TypeVar

from xiaowei_agent.contracts import AiConfig, FeishuConfig
from xiaowei_agent.interfaces.integration_config_file import (
    CONFIG_FILE_NAME,
    CONFIG_ROOT,
    LEGACY_CONFIG_FILE_NAME,
    IntegrationConfigError,
    IntegrationConfigMissingError,
    read_ai_config,
    read_feishu_config,
    read_legacy_integration_config,
    write_ai_config,
    write_feishu_config,
)

_DOMAIN_DIRECTORIES: Final[tuple[str, ...]] = ("ai", "feishu", "resources")

_T = TypeVar("_T", AiConfig, FeishuConfig)


class MigrationResult(StrEnum):
    """闭集结果码；每一个都对应一个不同的运维动作。"""

    NOTHING_TO_MIGRATE = "nothing_to_migrate"
    ALREADY_MIGRATED = "already_migrated"
    MIGRATED = "migrated"
    MIGRATION_REQUIRED = "migration_required"
    UNAVAILABLE = "unavailable"


class _MigrationStopError(Exception):
    """内部短路：携带唯一结果码，不携带任何文本。"""

    def __init__(self, result: MigrationResult) -> None:
        super().__init__(result.value)
        self.result = result


class _Absent:
    """某域文件不存在（``ENOENT``）。与"存在但坏"严格分开。"""


_ABSENT: Final = _Absent()


def _open_root(config_root: str) -> int:
    if not isinstance(config_root, str) or not os.path.isabs(config_root):
        raise _MigrationStopError(MigrationResult.UNAVAILABLE)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(config_root, flags)
    except OSError:
        raise _MigrationStopError(MigrationResult.UNAVAILABLE) from None


def _require_domain_directories(root_fd: int) -> None:
    for name in _DOMAIN_DIRECTORIES:
        try:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            raise _MigrationStopError(MigrationResult.UNAVAILABLE) from None
        if not stat.S_ISDIR(info.st_mode):
            raise _MigrationStopError(MigrationResult.UNAVAILABLE)


def _legacy_identity(root_fd: int) -> tuple[int, int] | None:
    """旧文件的 ``(st_dev, st_ino)``；不存在为 ``None``，非正规文件一律不可用。"""
    try:
        info = os.stat(LEGACY_CONFIG_FILE_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise _MigrationStopError(MigrationResult.UNAVAILABLE) from None
    if not stat.S_ISREG(info.st_mode):
        raise _MigrationStopError(MigrationResult.UNAVAILABLE)
    return info.st_dev, info.st_ino


def _existing(reader: Callable[[str], _T], path: str) -> _T | _Absent:
    """读一个已存在的新文件；存在但坏（含符号链接）即 ``migration_required``。"""
    try:
        return reader(path)
    except IntegrationConfigMissingError:
        return _ABSENT
    except IntegrationConfigError:
        raise _MigrationStopError(MigrationResult.MIGRATION_REQUIRED) from None


def _write_and_verify(
    *,
    reader: Callable[[str], _T],
    writer: Callable[[str, _T], None],
    path: str,
    expected: _T,
) -> None:
    try:
        writer(path, expected)
        written = reader(path)
    except IntegrationConfigError:
        raise _MigrationStopError(MigrationResult.UNAVAILABLE) from None
    if written != expected:
        raise _MigrationStopError(MigrationResult.UNAVAILABLE)


def _migrate(root_fd: int, config_root: str) -> MigrationResult:
    _require_domain_directories(root_fd)
    ai_path = os.path.join(config_root, "ai", CONFIG_FILE_NAME)
    feishu_path = os.path.join(config_root, "feishu", CONFIG_FILE_NAME)
    identity = _legacy_identity(root_fd)

    if identity is None:
        try:
            ai = _existing(read_ai_config, ai_path)
            feishu = _existing(read_feishu_config, feishu_path)
        except _MigrationStopError:
            # 没有旧文件时新文件损坏不是迁移冲突，而是一处需要人工查看的故障。
            return MigrationResult.UNAVAILABLE
        if isinstance(ai, _Absent) or isinstance(feishu, _Absent):
            return MigrationResult.NOTHING_TO_MIGRATE
        return MigrationResult.ALREADY_MIGRATED

    try:
        legacy = read_legacy_integration_config(
            os.path.join(config_root, LEGACY_CONFIG_FILE_NAME)
        )
    except IntegrationConfigMissingError:
        return MigrationResult.UNAVAILABLE
    except IntegrationConfigError:
        return MigrationResult.MIGRATION_REQUIRED
    if _legacy_identity(root_fd) != identity:
        return MigrationResult.UNAVAILABLE

    expected_ai = AiConfig(generation=legacy.generation, gemini=legacy.gemini)
    expected_feishu = FeishuConfig(generation=legacy.generation, feishu=legacy.feishu)

    # 先把两边都检查完，再写任何一份：否则"AI 缺、飞书冲突"会先写出半份新状态。
    current_ai = _existing(read_ai_config, ai_path)
    current_feishu = _existing(read_feishu_config, feishu_path)
    if not isinstance(current_ai, _Absent) and current_ai != expected_ai:
        return MigrationResult.MIGRATION_REQUIRED
    if not isinstance(current_feishu, _Absent) and current_feishu != expected_feishu:
        return MigrationResult.MIGRATION_REQUIRED

    if isinstance(current_ai, _Absent):
        _write_and_verify(
            reader=read_ai_config, writer=write_ai_config, path=ai_path, expected=expected_ai
        )
    if isinstance(current_feishu, _Absent):
        _write_and_verify(
            reader=read_feishu_config,
            writer=write_feishu_config,
            path=feishu_path,
            expected=expected_feishu,
        )

    # 删除前再确认一次：两域都在且相等，旧文件仍是当初读到的那个 inode。
    try:
        if read_ai_config(ai_path) != expected_ai:
            return MigrationResult.UNAVAILABLE
        if read_feishu_config(feishu_path) != expected_feishu:
            return MigrationResult.UNAVAILABLE
    except IntegrationConfigError:
        return MigrationResult.UNAVAILABLE
    if _legacy_identity(root_fd) != identity:
        return MigrationResult.UNAVAILABLE
    try:
        os.unlink(LEGACY_CONFIG_FILE_NAME, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError:
        return MigrationResult.UNAVAILABLE
    return MigrationResult.MIGRATED


def migrate_legacy_integration_config(*, config_root: str) -> MigrationResult:
    """执行一次迁移并返回闭集结果；不抛异常、不打印、不回显任何路径或内容。"""
    try:
        root_fd = _open_root(config_root)
    except _MigrationStopError as stop:
        return stop.result
    try:
        return _migrate(root_fd, config_root)
    except _MigrationStopError as stop:
        return stop.result
    except Exception:
        return MigrationResult.UNAVAILABLE
    finally:
        os.close(root_fd)


_SUCCESS: Final[frozenset[MigrationResult]] = frozenset(
    {
        MigrationResult.NOTHING_TO_MIGRATE,
        MigrationResult.ALREADY_MIGRATED,
        MigrationResult.MIGRATED,
    }
)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m xiaowei_agent.interfaces.integration_config_migrate``。

    路径不接受命令行参数：容器里的挂载点是固定的，让它可传等于开一条
    "迁移了另一个目录所以通过了"的路。也不接收 Secret——迁移只搬已有文件。
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print("migration: usage", flush=True)
        return 2
    result = migrate_legacy_integration_config(config_root=CONFIG_ROOT)
    print(f"migration: {result.value}", flush=True)
    return 0 if result in _SUCCESS else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["MigrationResult", "main", "migrate_legacy_integration_config"]

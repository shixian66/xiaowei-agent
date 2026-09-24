"""在启动 Web 之前证明三个配置域目录**真的**可用，且旧单文件已经迁走。

干净部署里最常见的失败不是代码错，是宿主机上的目录属主或权限不对：容器以 UID 10001
运行，目录属于宿主用户，于是管理员填完表单点保存才发现存不下——那时他已经改过密码、
配过凭据，却什么都没落盘。

这个模块把那次失败提前到 ``compose up`` 之前，并且**只**回答两件事：

1. 旧 ``integrations.json`` 还在吗？在就是 ``migration_required``，**什么都不写**——运行时
   不双读、不回落、不自动迁移，必须先显式运行一次性迁移器。
2. ``ai`` / ``feishu`` / ``resources`` 三个域目录各自能不能写？

- 每个目录写**自己的**哨兵 ``.preflight-probe-<domain>.json``，绝不是 ``config.json``：
  写到真实路径会留下一份 ``generation=1`` 的配置，干净部署的起点被预检自己污染。
- 哨兵只经 :func:`write_preflight_probe` 写：它走 Web 保存用的同一个原子 writer，这里不
  另抄一套 ``open/write/replace``。
- 成功与失败路径都删哨兵，退出前再断言三份真实 ``config.json`` **原样不动**。
- 只打印 ``preflight: ok`` 或一个闭集失败码，**绝不打印路径或文件内容**。
"""

import os
import stat
import sys
from enum import StrEnum
from typing import Final

from xiaowei_agent.interfaces.integration_config_file import (
    CONFIG_FILE_NAME,
    CONFIG_ROOT,
    LEGACY_CONFIG_FILE_NAME,
    IntegrationConfigError,
    PreflightProbeUnreadableError,
    write_preflight_probe,
)

DOMAIN_DIRECTORIES: Final[tuple[str, ...]] = ("ai", "feishu", "resources")

_OK: Final[str] = "preflight: ok"


def probe_file_name(domain: str) -> str:
    """某个域目录的哨兵文件名；以点开头，且与 ``config.json`` 永远不同。"""
    return f".preflight-probe-{domain}.json"


class PreflightFailure(StrEnum):
    """失败码闭集；每一条都对应一个不同的宿主机动作。"""

    DIRECTORY_MISSING = "preflight: directory_missing"
    DIRECTORY_NOT_WRITABLE = "preflight: directory_not_writable"
    PROBE_NOT_READABLE = "preflight: probe_not_readable"
    PROBE_NOT_REMOVABLE = "preflight: probe_not_removable"
    EXISTING_CONFIG_DISTURBED = "preflight: existing_config_disturbed"
    MIGRATION_REQUIRED = "preflight: migration_required"


def _is_real_directory(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _exists_without_following(path: str) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _config_fingerprint(path: str) -> tuple[int, int] | None:
    """已存在配置的 ``(inode, mtime_ns)``；不存在时为 ``None``。

    比"读出来再比内容"更强也更安全：内容相同但被重写过一次的文件同样是被动过，而且
    指纹不需要把明文凭据读进内存。
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return info.st_ino, info.st_mtime_ns


def _probe_one(directory: str, domain: str) -> str | None:
    probe = os.path.join(directory, probe_file_name(domain))
    outcome: str | None = None
    try:
        write_preflight_probe(probe)
    except PreflightProbeUnreadableError:
        outcome = PreflightFailure.PROBE_NOT_READABLE.value
    except (IntegrationConfigError, OSError):
        outcome = PreflightFailure.DIRECTORY_NOT_WRITABLE.value
    # 成功与失败都要删：留下的哨兵会在下次预检里变成一个"已存在的文件"。
    try:
        os.unlink(probe)
    except FileNotFoundError:
        pass
    except OSError:
        return PreflightFailure.PROBE_NOT_REMOVABLE.value
    return outcome


def run_preflight(root: str) -> str:
    """返回一行闭集结果；不抛异常、不打印。"""
    if not _is_real_directory(root):
        return PreflightFailure.DIRECTORY_MISSING.value
    if _exists_without_following(os.path.join(root, LEGACY_CONFIG_FILE_NAME)):
        # 新旧真源同时存在，或只剩旧文件：都必须先显式迁移，这里一个字节都不写。
        return PreflightFailure.MIGRATION_REQUIRED.value
    directories = {domain: os.path.join(root, domain) for domain in DOMAIN_DIRECTORIES}
    if not all(_is_real_directory(path) for path in directories.values()):
        return PreflightFailure.DIRECTORY_MISSING.value
    real_configs = [
        os.path.join(path, CONFIG_FILE_NAME) for path in directories.values()
    ]
    before = [_config_fingerprint(path) for path in real_configs]
    for domain, directory in directories.items():
        failure = _probe_one(directory, domain)
        if failure is not None:
            return failure
    if [_config_fingerprint(path) for path in real_configs] != before:
        # 预检**不得**改变"尚未配置"这个起点，也不得动一份已在被各进程使用的配置。
        return PreflightFailure.EXISTING_CONFIG_DISTURBED.value
    return _OK


def main() -> int:
    """``python -m xiaowei_agent.interfaces.config_preflight``。

    路径不接受命令行参数：容器里的挂载点是固定的，让它可传等于给自己开一条
    "预检了另一个目录所以通过了"的路。
    """
    line = run_preflight(CONFIG_ROOT)
    print(line, flush=True)
    return 0 if line == _OK else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DOMAIN_DIRECTORIES",
    "PreflightFailure",
    "main",
    "probe_file_name",
    "run_preflight",
]

"""在启动 Web 之前证明配置目录**真的**可用。

干净部署里最常见的失败不是代码错，是宿主机上的 `.config` 属主或权限不对：容器以
UID 10001 运行，目录属于宿主用户，于是管理员填完表单点保存才发现存不下——那时他
已经改过密码、配过凭据，却什么都没落盘。

这个模块把那次失败提前到 ``compose up`` 之前，并且**只**回答"能不能写"：

- 探测写的是**哨兵文件** ``.preflight-probe.json``，绝不是 `integrations.json`。
  写到真实路径会留下一份 ``generation=1`` 的配置，于是首次保存从 1 递增到 2，页面
  显示的代次与"第一份配置"对不上；更糟的是 ``read_or_absent`` 再也读不到"尚未
  配置"这个状态——干净部署的起点被预检自己污染了。
- 成功与失败路径都删哨兵文件，退出前再断言真实路径**原样不动**。
- 只打印 ``preflight: ok`` 或一个闭集失败码，**绝不打印文件内容**：这个目录里存的
  是明文 Provider 凭据，而这条命令的输出多半会被贴进工单。
"""

import os
import sys
from enum import StrEnum
from typing import Final

from xiaowei_agent.contracts import (
    FeishuIntegration,
    GeminiIntegration,
    IntegrationConfig,
)
from xiaowei_agent.interfaces.integration_config_file import (
    DEFAULT_INTEGRATION_CONFIG_PATH,
    IntegrationConfigError,
    read_integration_config,
    write_integration_config,
)

PROBE_FILE_NAME: Final[str] = ".preflight-probe.json"
"""哨兵文件名。

以点开头并带 ``.json`` 后缀，但**不是** `integrations.json`：各进程的读取路径是一个
固定文件名，不会把它当成配置。
"""

_OK: Final[str] = "preflight: ok"


class PreflightFailure(StrEnum):
    """失败码闭集；每一条都对应一个不同的宿主机动作。

    合并成一个"预检失败"会让运维无从下手——建目录、改属主、改权限、换路径是四件
    完全不同的事。
    """

    DIRECTORY_MISSING = "preflight: directory_missing"
    DIRECTORY_NOT_WRITABLE = "preflight: directory_not_writable"
    PROBE_NOT_READABLE = "preflight: probe_not_readable"
    PROBE_NOT_REMOVABLE = "preflight: probe_not_removable"
    EXISTING_CONFIG_DISTURBED = "preflight: existing_config_disturbed"


def _probe_config() -> IntegrationConfig:
    """哨兵内容：一份**不含任何凭据**的最小合法文档。

    走 ``write_integration_config`` 而不是自己 ``open().write()``，是为了让预检真正
    覆盖 Web 保存时那条路径——同目录临时文件、``0600``、``os.replace()``、目录
    fsync。自己写一遍等于测了一条生产中不存在的路径。
    """
    return IntegrationConfig(
        generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()
    )


def _config_fingerprint(path: str) -> tuple[int, int] | None:
    """已存在配置的 ``(inode, mtime_ns)``；不存在时为 ``None``。

    比"读出来再比内容"更强也更安全：内容相同但被重写过一次的文件同样是被动过，
    而且指纹不需要把明文凭据读进内存。
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return info.st_ino, info.st_mtime_ns


def run_preflight(directory: str) -> str:
    """返回一行闭集结果；不抛异常、不打印。"""
    if not os.path.isdir(directory):
        return PreflightFailure.DIRECTORY_MISSING.value
    real_config = os.path.join(directory, os.path.basename(DEFAULT_INTEGRATION_CONFIG_PATH))
    before = _config_fingerprint(real_config)
    probe = os.path.join(directory, PROBE_FILE_NAME)
    try:
        write_integration_config(probe, _probe_config())
    except (IntegrationConfigError, OSError):
        return PreflightFailure.DIRECTORY_NOT_WRITABLE.value
    outcome = _OK
    try:
        read_integration_config(probe)
    except (IntegrationConfigError, OSError):
        outcome = PreflightFailure.PROBE_NOT_READABLE.value
    # 成功与失败都要删：留下的哨兵会在下次预检里变成一个"已存在的文件"，
    # 也会让目录里多一份来路不明的 JSON。
    try:
        os.unlink(probe)
    except OSError:
        return PreflightFailure.PROBE_NOT_REMOVABLE.value
    if _config_fingerprint(real_config) != before:
        # 预检**不得**改变"尚未配置"这个起点，也不得动一份已在被各进程使用的配置。
        return PreflightFailure.EXISTING_CONFIG_DISTURBED.value
    return outcome


def main() -> int:
    """``python -m xiaowei_agent.interfaces.config_preflight``。

    路径不接受命令行参数：容器里的挂载点是固定的，让它可传等于给自己开一条
    "预检了另一个目录所以通过了"的路。
    """
    directory = os.path.dirname(DEFAULT_INTEGRATION_CONFIG_PATH)
    line = run_preflight(directory)
    print(line, flush=True)
    return 0 if line == _OK else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["PROBE_FILE_NAME", "PreflightFailure", "main", "run_preflight"]

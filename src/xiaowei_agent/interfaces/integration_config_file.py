"""`integrations.json` 的唯一读写边界。

与 :mod:`xiaowei_agent.interfaces.secret_file` 并列而不是复用它：那个 reader 面向
单行无控制字符的 credential，这里面向一份严格 schema 的 JSON 文档。两条路径各自
收敛，避免出现「同一个文件两种读法」。
"""

import json
import os
import stat
import tempfile
from typing import Final

from pydantic import ValidationError

from xiaowei_agent.contracts import IntegrationConfig

_MAX_CONFIG_BYTES: Final[int] = 65_536
DEFAULT_INTEGRATION_CONFIG_PATH: Final[str] = "/run/xiaowei-config/integrations.json"


class IntegrationConfigError(RuntimeError):
    """integration 配置无法安全读取或不满足 schema。"""


class IntegrationConfigMissingError(IntegrationConfigError):
    """路径本身不存在（``ENOENT``），即「尚未配置」。

    刻意做成基类的子类：既有 ``except IntegrationConfigError`` 的调用方因此仍然
    fail-closed，只有明确要区分「尚未配置」与「存在但坏」的调用方才捕获这一个。
    """


def read_integration_config(path: str) -> IntegrationConfig:
    """从绝对路径读取严格 schema 的 integration 配置。

    「不存在」与「存在但坏」的分流只做在 ``os.open`` 这一处，因为只有这里拿得到
    errno。调用方**不得**用 ``os.path.exists()`` 预检：它跟随符号链接，断链会报
    ``False``，于是指向不存在目标的符号链接被误判成「尚未配置」，绕过「符号链接
    一律故障」；``lexists()`` 只补这一例，仍分不出 ``EACCES`` / ``ELOOP``，也仍有
    检查与使用之间的时间窗。带 ``O_NOFOLLOW`` 的 ``os.open`` 对任何符号链接都抛
    ``ELOOP``，只有真正不存在的路径才抛 ``ENOENT``。
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise IntegrationConfigError("integration config unavailable")
    flags = os.O_RDONLY
    for name in ("O_NONBLOCK", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    descriptor: int | None = None
    payload = b""
    failed = False
    missing = False
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed = True
        else:
            payload = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        # 只有 ENOENT 才是「尚未配置」。必须排在下面这条之前，否则会被它吞掉。
        missing = True
    except (OSError, ValueError):
        failed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if missing:
        raise IntegrationConfigMissingError("integration config absent")
    if failed or not payload or len(payload) > _MAX_CONFIG_BYTES:
        raise IntegrationConfigError("integration config unavailable")
    try:
        document = json.loads(payload.decode("utf-8"))
        return IntegrationConfig.model_validate(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise IntegrationConfigError("integration config invalid") from None


def _to_document(config: IntegrationConfig) -> dict[str, object]:
    """构造**含 secret** 的落盘文档。

    不能用 ``model_dump()``：secret 字段 ``exclude=True``，dump 出来的文档少了
    凭据，写回去就把已保存的 Key 清空了。
    """
    gemini: dict[str, object] = {"enabled": config.gemini.enabled}
    if config.gemini.api_key is not None:
        gemini["api_key"] = config.gemini.api_key
    feishu: dict[str, object] = {"enabled": config.feishu.enabled}
    if config.feishu.app_id is not None:
        feishu["app_id"] = config.feishu.app_id
    if config.feishu.app_secret is not None:
        feishu["app_secret"] = config.feishu.app_secret
    return {"generation": config.generation, "gemini": gemini, "feishu": feishu}


def write_integration_config(path: str, config: IntegrationConfig) -> None:
    """同目录临时文件 + ``0600`` + ``os.replace()`` 原子替换。

    ``indent=2`` 不只是可读性：多行文档必然含换行，因此
    :func:`~xiaowei_agent.interfaces.secret_file.read_secret_file` 一定会拒绝它，
    旧 reader 不会意外成为这份 JSON 的第二条读取路径。
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise IntegrationConfigError("integration config unavailable")
    directory = os.path.dirname(path)
    payload = json.dumps(
        _to_document(config), ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    if len(payload) > _MAX_CONFIG_BYTES:
        raise IntegrationConfigError("integration config invalid")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".integrations-", suffix=".tmp")
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        os.close(descriptor)
        os.unlink(temporary)
        raise IntegrationConfigError("integration config unavailable") from None
    os.close(descriptor)
    try:
        os.replace(temporary, path)
    except OSError:
        os.unlink(temporary)
        raise IntegrationConfigError("integration config unavailable") from None
    directory_descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


__all__ = [
    "DEFAULT_INTEGRATION_CONFIG_PATH",
    "IntegrationConfigError",
    "IntegrationConfigMissingError",
    "read_integration_config",
    "write_integration_config",
]

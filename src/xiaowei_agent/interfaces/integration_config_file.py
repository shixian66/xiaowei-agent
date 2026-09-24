"""三域配置文件的唯一读写边界。

W4a 起 AI 与飞书各有一份固定文件、一个独立 ``generation``；resources 域只预留路径。
公开入口全部是**固定类型**的窄函数：不接受 schema class、动态 domain 字符串或自定义
serializer，也不返回任意 ``dict``。共同的文件安全逻辑保持私有，只有两份：一个严格
读取器、一个原子写入器——预检的哨兵写入也只能经 :func:`write_preflight_probe` 走同一个
写入器，不另抄一套 ``open/write/replace``。

与 :mod:`xiaowei_agent.interfaces.secret_file` 并列而不是复用它：那个 reader 面向
单行无控制字符的 credential，这里面向严格 schema 的 JSON 文档。

旧 ``integrations.json`` 只剩 :func:`read_legacy_integration_config` 这一条读取路径，
且只准一次性迁移器调用（``tests/contract/test_w4_config_domain_contracts.py``）；
这里**没有**旧文档的写入器，运行时也没有任何回落到旧文件的分支。
"""

import json
import os
import stat
import tempfile
from typing import Final

from pydantic import ValidationError

from xiaowei_agent.application.integration_config_service import (
    IntegrationConfigUnreadableError,
    IntegrationConfigWriteError,
)
from xiaowei_agent.contracts import (
    AiConfig,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
    IntegrationConfig,
)

_MAX_CONFIG_BYTES: Final[int] = 65_536

CONFIG_ROOT: Final[str] = "/run/xiaowei-config"
"""容器内三个域目录的共同父路径。**不得**作为挂载目标：consumer 只挂自己的域目录。"""

CONFIG_FILE_NAME: Final[str] = "config.json"
LEGACY_CONFIG_FILE_NAME: Final[str] = "integrations.json"
"""旧单文件的名字。只有迁移器与预检用它来**发现**旧文件；运行时不读它。"""

DEFAULT_AI_CONFIG_PATH: Final[str] = f"{CONFIG_ROOT}/ai/{CONFIG_FILE_NAME}"
DEFAULT_FEISHU_CONFIG_PATH: Final[str] = f"{CONFIG_ROOT}/feishu/{CONFIG_FILE_NAME}"
DEFAULT_RESOURCES_CONFIG_PATH: Final[str] = f"{CONFIG_ROOT}/resources/{CONFIG_FILE_NAME}"
"""W4a 只预留：W4b 才有 resources 文档契约与读写器。"""

_PROBE_DOCUMENT: Final[dict[str, object]] = {"preflight_probe": 1}
"""哨兵内容：不含任何凭据，也**不是**任何一个域的合法文档。

预检残留的哨兵因此永远不会被某个域的 reader 当成配置读进去。
"""


class IntegrationConfigError(RuntimeError):
    """配置文件无法安全读取、写入或不满足 schema。异常文本固定，不带路径或内容。"""


class IntegrationConfigMissingError(IntegrationConfigError):
    """路径本身不存在（``ENOENT``），即「尚未配置」。

    刻意做成基类的子类：既有 ``except IntegrationConfigError`` 的调用方因此仍然
    fail-closed，只有明确要区分「尚未配置」与「存在但坏」的调用方才捕获这一个。
    """


class PreflightProbeUnreadableError(IntegrationConfigError):
    """哨兵写进去了，却没能原样读回。"""

    def __init__(self) -> None:
        super().__init__("preflight probe unreadable")


def _read_document(path: str) -> object:
    """严格读取一个 JSON 文档；只有 ``ENOENT`` 算「不存在」。

    「不存在」与「存在但坏」的分流只做在 ``os.open`` 这一处，因为只有这里拿得到
    errno。调用方**不得**用 ``os.path.exists()`` 预检：它跟随符号链接，断链会报
    ``False``，于是指向不存在目标的符号链接被误判成「尚未配置」。带 ``O_NOFOLLOW``
    的 ``os.open`` 对任何符号链接都抛 ``ELOOP``，只有真正不存在的路径才抛 ``ENOENT``。
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
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrationConfigError("integration config invalid") from None


def _atomic_write(path: str, document: dict[str, object], *, prefix: str) -> None:
    """同目录临时文件 + ``0600`` + ``fsync`` + ``os.replace()`` + 目录 ``fsync``。

    任一步失败都抛闭集 :class:`IntegrationConfigError`，替换前失败会清掉临时文件。目录
    ``fsync`` 失败发生在替换**之后**：新内容可能已经可见但持久性未知，此时仍报失败
    而不是成功——调用方的两阶段审计因此停在 ``FAILED``（或只剩 ``STARTED``），诚实
    表达"结果未知"，而不是回报一次无法证明已持久化的成功。

    ``indent=2`` 不只是可读性：多行文档必然含换行，因此
    :func:`~xiaowei_agent.interfaces.secret_file.read_secret_file` 一定会拒绝它，
    单行 reader 不会意外成为这份 JSON 的第二条读取路径。
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise IntegrationConfigError("integration config unavailable")
    directory = os.path.dirname(path)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    )
    if len(payload) > _MAX_CONFIG_BYTES:
        raise IntegrationConfigError("integration config invalid")
    try:
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    except OSError:
        raise IntegrationConfigError("integration config unavailable") from None
    replaced = False
    try:
        try:
            os.fchmod(descriptor, 0o600)
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        replaced = True
        directory_descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        if not replaced:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise IntegrationConfigError("integration config unavailable") from None


def _gemini_section(gemini: GeminiIntegration) -> dict[str, object]:
    """**含 secret** 的落盘片段。

    不能用 ``model_dump()``：secret 字段 ``exclude=True``，dump 出来的文档少了
    凭据，写回去就把已保存的 Key 清空了。
    """
    section: dict[str, object] = {"enabled": gemini.enabled}
    if gemini.api_key is not None:
        section["api_key"] = gemini.api_key
    return section


def _feishu_section(feishu: FeishuIntegration) -> dict[str, object]:
    section: dict[str, object] = {"enabled": feishu.enabled}
    if feishu.app_id is not None:
        section["app_id"] = feishu.app_id
    if feishu.app_secret is not None:
        section["app_secret"] = feishu.app_secret
    return section


def read_ai_config(path: str = DEFAULT_AI_CONFIG_PATH) -> AiConfig:
    """读取 AI 域；不存在抛 :class:`IntegrationConfigMissingError`，其余一律故障。"""
    document = _read_document(path)
    try:
        return AiConfig.model_validate(document)
    except ValidationError:
        raise IntegrationConfigError("integration config invalid") from None


def write_ai_config(path: str, config: AiConfig) -> None:
    """原子写入 AI 域；不触碰任何其他域的文件。"""
    if not isinstance(config, AiConfig):
        raise IntegrationConfigError("integration config invalid")
    _atomic_write(
        path,
        {"generation": config.generation, "gemini": _gemini_section(config.gemini)},
        prefix=".ai-",
    )


def read_feishu_config(path: str = DEFAULT_FEISHU_CONFIG_PATH) -> FeishuConfig:
    """读取飞书域；不存在抛 :class:`IntegrationConfigMissingError`，其余一律故障。"""
    document = _read_document(path)
    try:
        return FeishuConfig.model_validate(document)
    except ValidationError:
        raise IntegrationConfigError("integration config invalid") from None


def write_feishu_config(path: str, config: FeishuConfig) -> None:
    """原子写入飞书域；不触碰任何其他域的文件。"""
    if not isinstance(config, FeishuConfig):
        raise IntegrationConfigError("integration config invalid")
    _atomic_write(
        path,
        {"generation": config.generation, "feishu": _feishu_section(config.feishu)},
        prefix=".feishu-",
    )


def read_legacy_integration_config(path: str) -> IntegrationConfig:
    """严格读取旧组合文档。**只供一次性迁移器调用**，运行时不得引用。"""
    document = _read_document(path)
    try:
        return IntegrationConfig.model_validate(document)
    except ValidationError:
        raise IntegrationConfigError("integration config invalid") from None


def write_preflight_probe(path: str) -> None:
    """经同一个原子写入器写一份哨兵，再用同一个严格读取器读回核对。

    写不进去抛 :class:`IntegrationConfigError`，读不回来抛
    :class:`PreflightProbeUnreadableError`；调用方不需要知道任何文件原语。
    """
    _atomic_write(path, dict(_PROBE_DOCUMENT), prefix=".preflight-")
    try:
        document = _read_document(path)
    except IntegrationConfigError:
        raise PreflightProbeUnreadableError from None
    if document != _PROBE_DOCUMENT:
        raise PreflightProbeUnreadableError


class FileIntegrationConfigRepository:
    """application ``IntegrationConfigRepository`` 的文件 adapter。

    只把本模块的窄入口翻译成 application 的闭集错误：不存在是 ``None``，存在但坏是
    :class:`IntegrationConfigUnreadableError`，写失败是 :class:`IntegrationConfigWriteError`。
    路径在构造时由 composition root 固定，方法不接收路径、schema 或 domain。
    """

    def __init__(
        self,
        *,
        ai_path: str = DEFAULT_AI_CONFIG_PATH,
        feishu_path: str = DEFAULT_FEISHU_CONFIG_PATH,
    ) -> None:
        self._ai_path = ai_path
        self._feishu_path = feishu_path

    def read_ai(self) -> AiConfig | None:
        try:
            return read_ai_config(self._ai_path)
        except IntegrationConfigMissingError:
            return None
        except IntegrationConfigError:
            raise IntegrationConfigUnreadableError from None

    def write_ai(self, *, config: AiConfig) -> None:
        try:
            write_ai_config(self._ai_path, config)
        except IntegrationConfigError:
            raise IntegrationConfigWriteError from None

    def read_feishu(self) -> FeishuConfig | None:
        try:
            return read_feishu_config(self._feishu_path)
        except IntegrationConfigMissingError:
            return None
        except IntegrationConfigError:
            raise IntegrationConfigUnreadableError from None

    def write_feishu(self, *, config: FeishuConfig) -> None:
        try:
            write_feishu_config(self._feishu_path, config)
        except IntegrationConfigError:
            raise IntegrationConfigWriteError from None


__all__ = [
    "CONFIG_FILE_NAME",
    "CONFIG_ROOT",
    "DEFAULT_AI_CONFIG_PATH",
    "DEFAULT_FEISHU_CONFIG_PATH",
    "DEFAULT_RESOURCES_CONFIG_PATH",
    "LEGACY_CONFIG_FILE_NAME",
    "FileIntegrationConfigRepository",
    "IntegrationConfigError",
    "IntegrationConfigMissingError",
    "PreflightProbeUnreadableError",
    "read_ai_config",
    "read_feishu_config",
    "read_legacy_integration_config",
    "write_ai_config",
    "write_feishu_config",
    "write_preflight_probe",
]

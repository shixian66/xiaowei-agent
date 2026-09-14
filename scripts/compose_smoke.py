"""API/Worker/PostgreSQL fake 闭环与 M7 渠道进程离线装配验收。"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from xiaowei_agent.interfaces import FEISHU_PROVIDER_ORIGIN

_ROOT = Path(__file__).resolve().parents[1]
_SMOKE_INPUT_ROOT = _ROOT / ".secrets"
_API_READY_URL = "http://127.0.0.1:8000/readyz"
_WEB_READY_URL = "http://127.0.0.1:8080/readyz"
_WEB_OAUTH_START_PATH = "/oauth/feishu/start"
_WEB_PUBLIC_HOST = "sso.example.invalid"
_WEB_DIRECT_HOST = "127.0.0.1:8080"
_WEB_APP_ID = "cli_smoke_fake_app"
_WEB_CALLBACK_URL = f"https://{_WEB_PUBLIC_HOST}/oauth/feishu/callback"
_WEB_AUTHORIZATION_PATH = "/open-apis/authen/v1/index"
_WEB_FORBIDDEN_BODY = b'{"error":{"code":"forbidden"}}'
_MAX_WEB_RESPONSE_BYTES = 32_768
_OAUTH_STATE_RE = re.compile(r"[A-Za-z0-9_-]{16,512}")
_OAUTH_STATE_COOKIE_NAME = "__Host-xiaowei-oauth-state"
_OAUTH_STATE_COOKIE_PAIR_RE = re.compile(
    rf"(?:^|[;,]\s*){re.escape(_OAUTH_STATE_COOKIE_NAME)}\s*="
)
_EUID_PROBE_CODE = "import os,sys;sys.stdout.write(str(os.geteuid()))"
_COMMAND_TIMEOUT = 180.0
_MINIMUM_COMPOSE_VERSION = (2, 24, 4)
_GEMINI_HOST_SECRET_FILE_ENV = "GEMINI_API_" + "KEY_FILE"
_CONFIG_DESTINATION = "/run/xiaowei-config"
_CONFIG_FILE_NAME = "integrations.json"
# 只读挂载配置目录的三个消费进程；web-app 单独可写，api/migrate/postgres 完全不挂。
_CONFIG_READ_ONLY_SERVICES = ("worker", "feishu-listener", "channel-worker")
_POSTGRES_SECRET_DESTINATION = "/run/secrets/postgres_" + "password"
_MODEL_AUDIT_SERVICES = (
    "postgres",
    "migrate",
    "api",
    "worker",
    "feishu-listener",
    "channel-worker",
    "web-app",
)
_TERMINAL = {"succeeded", "failed", "rejected", "canceled", "indeterminate"}
_NON_SUCCESS_CODES = {
    "failed": "SMOKE_TASK_FAILED",
    "rejected": "SMOKE_TASK_REJECTED",
    "canceled": "SMOKE_TASK_CANCELED",
    "indeterminate": "SMOKE_TASK_INDETERMINATE",
}
_TEXT = "检查最近三十分钟慢查询"
_PROMETHEUS_TEXT = "查告警 HostHighCpu 在 node-1.example.com:9100 的证据"
_ASSET_TEXT = "查资产 hostname=node-1.example.com"
_MIGRATION_FAILURE_CODES = (
    ("xiaowei-migrate: configuration_error", "SMOKE_MIGRATION_CONFIGURATION_FAILED"),
    ("xiaowei-migrate: database_unavailable", "SMOKE_MIGRATION_DATABASE_UNAVAILABLE"),
    ("xiaowei-migrate: database_error", "SMOKE_MIGRATION_DATABASE_ERROR"),
    ("xiaowei-migrate: migration_command_error", "SMOKE_MIGRATION_COMMAND_ERROR"),
    ("xiaowei-migrate: io_error", "SMOKE_MIGRATION_IO_ERROR"),
)
_DISABLED_CHANNEL_ENTRYPOINTS = (
    "xiaowei_agent.interfaces.feishu_listener",
    "xiaowei_agent.interfaces.feishu_worker",
    "xiaowei_agent.interfaces.web_app",
)


class SmokeError(RuntimeError):
    """只携固定 smoke 错误码，不携带 Docker/API 输出。"""


def _safe_add_fixed_note(error: BaseException, note: str) -> None:
    """追加固定 note；异常对象拒绝记录时也绝不影响原始失败。"""
    try:
        error.add_note(note)
    except BaseException:
        return


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    argv: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- argv[0] 由 shutil.which 解析为绝对路径
        list(argv),
        shell=False,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def project_name() -> str:
    return f"xiaowei_m5_smoke_{uuid.uuid4().hex}"


def _resolve_compose_command(
    *, docker: str, runner: CommandRunner
) -> tuple[str, ...]:
    """选择实际可工作的 Compose CLI，且不回显探测输出。"""
    candidates: list[tuple[str, ...]] = [(docker, "compose")]
    standalone = shutil.which("docker-compose")
    if standalone is not None:
        candidates.append((standalone,))
    for command in candidates:
        try:
            result = runner((*command, "version", "--short"), timeout=15.0)
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.fullmatch(
            r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", result.stdout.strip()
        )
        if (
            match is not None
            and tuple(map(int, match.groups())) >= _MINIMUM_COMPOSE_VERSION
        ):
            return command
    raise SmokeError("SMOKE_COMPOSE_NOT_FOUND") from None


def _project_label(name: str) -> str:
    return f"label=com.docker.compose.project={name}"


def _preflight(
    *, docker: str, runner: CommandRunner, project: str
) -> None:
    label = _project_label(project)
    queries = (
        (docker, "ps", "-aq", "--filter", label),
        (docker, "network", "ls", "-q", "--filter", label),
        (docker, "volume", "ls", "-q", "--filter", label),
    )
    for argv in queries:
        failure: SmokeError | None = None
        try:
            result = runner(argv, timeout=15.0)
        except (OSError, subprocess.SubprocessError):
            failure = SmokeError("SMOKE_PREFLIGHT_COMMAND_FAILED")
        if failure is not None:
            raise failure
        if result.stdout.strip():
            raise SmokeError("SMOKE_PROJECT_COLLISION")


@dataclass(frozen=True)
class _OwnedInput:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class _PrivateInputNamespace:
    root: Path
    path: Path
    root_device: int
    root_inode: int
    device: int
    inode: int


@dataclass(frozen=True)
class _SmokeInputs:
    namespace: _PrivateInputNamespace
    # 配置目录**整目录**挂进容器，因此必须是独占的一个命名空间：与 postgres 口令、
    # override 文档同目录时，那两份也会一起出现在 worker 的 /run/xiaowei-config 下。
    config_namespace: _PrivateInputNamespace
    sensitive_values: tuple[str, ...]
    owned_inputs: tuple[_OwnedInput, ...]
    config_inputs: tuple[_OwnedInput, ...]
    override_path: Path
    config_source: Path


def _cleanup_unreturned_input(
    *,
    directory: int,
    descriptor: int | None,
    name: str,
    created: bool,
    identity: tuple[int, int] | None,
    primary_error: BaseException | None,
) -> None:
    """清理尚未移交给调用方的 inode，且不覆盖原始异常。"""
    cleanup_failed = False
    created_identity = identity
    if created and created_identity is None and descriptor is not None:
        try:
            created_stat = os.fstat(descriptor)
            created_identity = (created_stat.st_dev, created_stat.st_ino)
        except BaseException:  # 保留正在传播的 KeyboardInterrupt 等原始异常
            cleanup_failed = True
    if descriptor is not None:
        try:
            os.close(descriptor)
        except BaseException:  # 同上；错误细节不得越过 smoke 边界
            cleanup_failed = True
    if created and created_identity is not None:
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except BaseException:
            cleanup_failed = True
        else:
            if (current.st_dev, current.st_ino) == created_identity:
                try:
                    os.unlink(name, dir_fd=directory)
                except BaseException:
                    cleanup_failed = True
    try:
        os.close(directory)
    except BaseException:
        cleanup_failed = True
    if cleanup_failed:
        if primary_error is not None:
            _safe_add_fixed_note(primary_error, "SMOKE_INPUT_CLEANUP_FAILED")
            return
        raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED") from None


def _create_input(path: Path, content: str) -> _OwnedInput:
    try:
        payload = content.encode("utf-8")
    except UnicodeError:
        raise SmokeError("SMOKE_INPUT_CREATE_FAILED") from None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID") from None
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory = os.open(path.parent, directory_flags)
    except OSError:
        raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID") from None
    try:
        parent_stat = os.fstat(directory)
    except OSError:
        os.close(directory)
        raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID") from None
    parent_mode = stat.S_IMODE(parent_stat.st_mode)
    if parent_mode != 0o700:
        os.close(directory)
        raise SmokeError("SMOKE_INPUT_DIRECTORY_PERMISSIONS")
    descriptor: int | None = None
    created = False
    identity: tuple[int, int] | None = None
    owned: _OwnedInput | None = None
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory,
            )
            created = descriptor is not None
        except FileExistsError:
            raise SmokeError("SMOKE_INPUT_ALREADY_EXISTS") from None
        except OSError:
            raise SmokeError("SMOKE_INPUT_CREATE_FAILED") from None
        file_stat = os.fstat(descriptor)
        identity = (file_stat.st_dev, file_stat.st_ino)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o444)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise OSError
        owned = _OwnedInput(
            path=path,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
        )
    except OSError:
        raise SmokeError("SMOKE_INPUT_CREATE_FAILED") from None
    finally:
        primary_error = sys.exception()
        if owned is None or primary_error is not None:
            _cleanup_unreturned_input(
                directory=directory,
                descriptor=descriptor,
                name=path.name,
                created=created,
                identity=identity,
                primary_error=primary_error,
            )
        else:
            try:
                if descriptor is None:
                    raise RuntimeError("missing created input descriptor")
                os.close(descriptor)
                descriptor = None
                os.close(directory)
            except BaseException as error:
                _cleanup_unreturned_input(
                    directory=directory,
                    descriptor=descriptor,
                    name=path.name,
                    created=created,
                    identity=identity,
                    primary_error=error,
                )
                raise
    if owned is None:
        raise RuntimeError("missing created input ownership")
    return owned


def _create_secret(path: Path) -> tuple[str, _OwnedInput]:
    value = secrets.token_urlsafe(32)
    owned = _create_input(path, f"{value}\n")
    return value, owned


def _create_private_input_namespace(root: Path) -> _PrivateInputNamespace:
    """创建单次 smoke 独占的不可预测 0700 目录。"""
    root = Path(os.path.abspath(root))
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID") from None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_descriptor = os.open(root, flags)
    except OSError:
        raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID") from None
    name = f"compose-smoke-{uuid.uuid4().hex}"
    created = False
    try:
        root_stat = os.fstat(root_descriptor)
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise SmokeError("SMOKE_INPUT_DIRECTORY_PERMISSIONS")
        os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
        created = True
        private_stat = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(private_stat.st_mode):
            raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID")
        if stat.S_IMODE(private_stat.st_mode) != 0o700:
            raise SmokeError("SMOKE_INPUT_DIRECTORY_PERMISSIONS")
        return _PrivateInputNamespace(
            root=root,
            path=root / name,
            root_device=root_stat.st_dev,
            root_inode=root_stat.st_ino,
            device=private_stat.st_dev,
            inode=private_stat.st_ino,
        )
    except SmokeError:
        raise
    except OSError:
        raise SmokeError("SMOKE_INPUT_DIRECTORY_INVALID") from None
    finally:
        if created and sys.exception() is not None:
            try:
                os.rmdir(name, dir_fd=root_descriptor)
            except OSError:
                pass
        os.close(root_descriptor)


def _config_mount(source: Path, *, read_only: bool) -> dict[str, object]:
    """把基础文件里的 ``./.config`` 绑定改指到本次 smoke 的私有目录。

    Compose 按**目标路径**合并卷：同一个 ``/run/xiaowei-config`` 目标会被 override
    整条替换，而不是并列出两条。因此这里既不需要 ``!override``，也不会在容器里
    留下两个互相打架的挂载点。
    """
    mount: dict[str, object] = {
        "type": "bind",
        "source": str(source),
        "target": _CONFIG_DESTINATION,
        "bind": {"create_host_path": False},
    }
    if read_only:
        mount["read_only"] = True
    return mount


def _input_override_document(
    *, postgres: Path, config_directory: Path, identity: Path
) -> str:
    """本次 smoke 的输入 override。

    Provider 凭据不再是 Docker secret：它们躺在一份合成的
    ``integrations.json`` 里，和真实部署走同一条读取路径。
    """
    identity_mount = {
        "type": "bind",
        "source": str(identity),
        "target": "/run/config/feishu-identities.json",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    services: dict[str, object] = {
        name: {"volumes": [_config_mount(config_directory, read_only=True)]}
        for name in _CONFIG_READ_ONLY_SERVICES
    }
    services["feishu-listener"] = {
        "volumes": [
            _config_mount(config_directory, read_only=True),
            identity_mount,
        ]
    }
    services["web-app"] = {
        "volumes": [
            _config_mount(config_directory, read_only=False),
            identity_mount,
        ]
    }
    return (
        json.dumps(
            {
                "secrets": {"postgres_password": {"file": str(postgres)}},
                "services": services,
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def _remove_private_input_namespace(
    namespace: _PrivateInputNamespace, inputs: Sequence[_OwnedInput]
) -> None:
    """原子隔离目录后只清理已验证的已知文件，不递归删除未知树。"""
    try:
        root = os.open(
            namespace.root,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED") from None
    quarantine_name = f".compose-smoke-retired-{uuid.uuid4().hex}"
    retired = False
    failure = False
    retired_directory: int | None = None
    try:
        root_stat = os.fstat(root)
        if (root_stat.st_dev, root_stat.st_ino) != (
            namespace.root_device,
            namespace.root_inode,
        ):
            raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED")
        os.rename(
            namespace.path.name,
            quarantine_name,
            src_dir_fd=root,
            dst_dir_fd=root,
        )
        retired = True
        retired_directory = os.open(
            quarantine_name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root,
        )
        retired_stat = os.fstat(retired_directory)
        if (retired_stat.st_dev, retired_stat.st_ino) != (
            namespace.device,
            namespace.inode,
        ):
            raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED")
        for owned in inputs:
            try:
                current = os.stat(
                    owned.path.name,
                    dir_fd=retired_directory,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (
                    owned.device,
                    owned.inode,
                ):
                    failure = True
                    continue
                os.unlink(owned.path.name, dir_fd=retired_directory)
            except FileNotFoundError:
                continue
            except OSError:
                failure = True
        os.close(retired_directory)
        retired_directory = None
        try:
            os.rmdir(quarantine_name, dir_fd=root)
        except OSError:
            failure = True
        if failure:
            raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED")
    except FileNotFoundError:
        if not retired:
            return
        raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED") from None
    except OSError:
        raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED") from None
    finally:
        if retired_directory is not None:
            os.close(retired_directory)
        os.close(root)


def _synthetic_integration_config(*, gemini_value: str, feishu_value: str) -> str:
    """一份**合成的** `integrations.json`；值是随机生成的假串，不来自任何真实账号。"""
    return (
        json.dumps(
            {
                "generation": 1,
                "gemini": {"enabled": False, "api_key": gemini_value},
                "feishu": {
                    "enabled": False,
                    "app_id": _WEB_APP_ID,
                    "app_secret": feishu_value,
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _create_smoke_inputs(*, input_root: Path) -> _SmokeInputs:
    namespace = _create_private_input_namespace(input_root)
    config_namespace = _create_private_input_namespace(input_root)
    postgres_secret = namespace.path / "postgres_password"
    identity = namespace.path / "feishu-identities.json"
    override = namespace.path / "compose-smoke-inputs.json"
    config_file = config_namespace.path / _CONFIG_FILE_NAME
    created: list[_OwnedInput] = []
    config_created: list[_OwnedInput] = []
    try:
        postgres_value, postgres_owned = _create_secret(postgres_secret)
        created.append(postgres_owned)
        gemini_value = "AIza" + secrets.token_urlsafe(32)
        feishu_value = secrets.token_urlsafe(32)
        config_created.append(
            _create_input(
                config_file,
                _synthetic_integration_config(
                    gemini_value=gemini_value, feishu_value=feishu_value
                ),
            )
        )
        identity_owned = _create_input(
            identity,
            json.dumps(
                {
                    "version": 1,
                    "tenant_id": "dev-local",
                    "environment_id": "dev",
                    "entries": [],
                },
                separators=(",", ":"),
            )
            + "\n",
        )
        created.append(identity_owned)
        override_owned = _create_input(
            override,
            _input_override_document(
                postgres=postgres_secret,
                config_directory=config_namespace.path,
                identity=identity,
            ),
        )
        created.append(override_owned)
    except BaseException as exc:
        # 两个命名空间都要清，但诊断**只挂一次**：同一条闭集码重复两遍不多给任何
        # 信息，只会让读者以为发生了两类不同的失败。
        cleanup_failed = False
        for target, owned in (
            (namespace, created),
            (config_namespace, config_created),
        ):
            try:
                _remove_private_input_namespace(target, owned)
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            _safe_add_fixed_note(exc, "SMOKE_INPUT_CLEANUP_FAILED")
        raise
    return _SmokeInputs(
        namespace=namespace,
        config_namespace=config_namespace,
        sensitive_values=(postgres_value, feishu_value, gemini_value),
        owned_inputs=tuple(created),
        config_inputs=tuple(config_created),
        override_path=override,
        config_source=config_namespace.path,
    )


@dataclass
class ComposeSession:
    docker: str
    runner: CommandRunner
    project: str
    files: tuple[Path, ...]
    compose_command: tuple[str, ...]
    sensitive_values: tuple[str, ...] = ()
    config_source: Path | None = None
    up_started: bool = False
    failure_code: str = "SMOKE_COMPOSE_COMMAND_FAILED"
    _resource_owner: ComposeSession | None = None

    def argv(self, *arguments: str) -> list[str]:
        command = list(self.compose_command)
        command.extend(("--profile", "m7-channels", "-p", self.project))
        for path in self.files:
            command.extend(("-f", str(path)))
        command.extend(arguments)
        return command

    def derive(
        self, *, files: tuple[Path, ...], failure_code: str
    ) -> ComposeSession:
        """为同一 project 派生 override session，保持 CLI 与归属不变。"""
        return ComposeSession(
            docker=self.docker,
            runner=self.runner,
            project=self.project,
            files=files,
            compose_command=self.compose_command,
            sensitive_values=self.sensitive_values,
            config_source=self.config_source,
            up_started=self.up_started,
            failure_code=failure_code,
            _resource_owner=self._resource_owner or self,
        )

    def run(
        self,
        *arguments: str,
        timeout: float = _COMMAND_TIMEOUT,
        failure_code: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if "up" in arguments or "create" in arguments:
            self.up_started = True
            if self._resource_owner is not None:
                self._resource_owner.up_started = True
        return self.run_docker(
            self.argv(*arguments),
            timeout=timeout,
            failure_code=failure_code,
        )

    def run_docker(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        failure_code: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """运行 Docker argv；只把当前固定阶段码带过脱敏边界。"""
        failure: SmokeError | None = None
        try:
            return self.runner(argv, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            failure = SmokeError(failure_code or self.failure_code)
        if failure is not None:
            raise failure
        raise RuntimeError("unreachable Docker command outcome")


Workflow = Callable[[ComposeSession], None]


def run_smoke(
    *,
    docker: str,
    compose_command: tuple[str, ...],
    runner: CommandRunner = _default_runner,
    workflow: Workflow,
    input_root: Path = _SMOKE_INPUT_ROOT,
) -> None:
    """只清理本次随机 Compose project 与私有输入命名空间。"""
    project = project_name()
    _preflight(docker=docker, runner=runner, project=project)
    smoke_inputs = _create_smoke_inputs(input_root=input_root)
    session = ComposeSession(
        docker=docker,
        runner=runner,
        project=project,
        files=(
            _ROOT / "docker-compose.yml",
            _ROOT / "docker-compose.smoke.yml",
            smoke_inputs.override_path,
        ),
        compose_command=compose_command,
        sensitive_values=smoke_inputs.sensitive_values,
        config_source=smoke_inputs.config_source,
    )
    primary_error: BaseException | None = None
    try:
        workflow(session)
    except BaseException as exc:
        primary_error = exc
    if session.up_started:
        try:
            session.failure_code = "SMOKE_CLEANUP_COMMAND_FAILED"
            session.run(
                "down",
                "--volumes",
                "--remove-orphans",
                timeout=60.0,
            )
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
            else:
                _safe_add_fixed_note(
                    primary_error,
                    "SMOKE_CLEANUP_COMMAND_FAILED",
                )
    input_cleanup_noted = False
    for target, owned in (
        (smoke_inputs.namespace, smoke_inputs.owned_inputs),
        (smoke_inputs.config_namespace, smoke_inputs.config_inputs),
    ):
        try:
            _remove_private_input_namespace(target, owned)
        except BaseException as exc:
            if primary_error is None:
                # 这一条**就是**被抛出的异常；再给它挂一条同名的 note 只是重复。
                primary_error = exc
                input_cleanup_noted = True
            elif not input_cleanup_noted:
                input_cleanup_noted = True
                _safe_add_fixed_note(primary_error, "SMOKE_INPUT_CLEANUP_FAILED")
    if primary_error is not None:
        raise primary_error


def _task(
    session: ComposeSession,
    *arguments: str,
    failure_code: str = "SMOKE_CLI_COMMAND_FAILED",
) -> dict[str, object]:
    result = session.run(
        "exec",
        "-T",
        "api",
        "xiaowei",
        *arguments,
        timeout=30.0,
        failure_code=failure_code,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SmokeError("SMOKE_CLI_PROTOCOL_ERROR") from None
    if not isinstance(value, dict):
        raise SmokeError("SMOKE_CLI_PROTOCOL_ERROR")
    return value


def _task_id(value: dict[str, object]) -> str:
    task_id = value.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise SmokeError("SMOKE_TASK_ID_MISSING")
    return task_id


def _wait_task(session: ComposeSession, task_id: str, *, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _task(
            session,
            "task",
            "get",
            task_id,
            failure_code="SMOKE_CLI_QUERY_FAILED",
        )
        if value.get("status") in _TERMINAL:
            return value
        time.sleep(0.5)
    raise SmokeError("SMOKE_TASK_TIMEOUT")


def _require_succeeded(value: dict[str, object]) -> None:
    status = value.get("status")
    if status == "succeeded":
        return
    code = _NON_SUCCESS_CODES.get(status) if isinstance(status, str) else None
    raise SmokeError(code or "SMOKE_TASK_STATUS_INVALID")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        """Host readiness 不允许把本地探测重定向到第二跳。"""
        return None


def _build_readiness_opener() -> urllib.request.OpenerDirector:
    """构造不读取宿主代理变量、也不跟随重定向的本地 opener。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


def _wait_ready(*, web: bool = False, timeout: float) -> None:
    url = _WEB_READY_URL if web else _API_READY_URL
    opener = _build_readiness_opener()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=2.0) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise SmokeError("SMOKE_READINESS_TIMEOUT")


def _container_id(
    session: ComposeSession, service: str, *, include_stopped: bool = False
) -> str:
    arguments = ("ps", "--all", "--quiet", service) if include_stopped else (
        "ps",
        "--quiet",
        service,
    )
    value = session.run(*arguments, timeout=15.0).stdout.strip()
    if not value or "\n" in value:
        raise SmokeError("SMOKE_CONTAINER_ID_INVALID")
    return value


def _container_env(session: ComposeSession, service: str) -> set[str]:
    container = _container_id(session, service)
    result = session.run_docker(
        (session.docker, "inspect", "--format", "{{json .Config.Env}}", container),
        timeout=15.0,
    )
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SmokeError("SMOKE_INSPECT_PROTOCOL_ERROR") from None
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise SmokeError("SMOKE_INSPECT_PROTOCOL_ERROR")
    return set(values)


def _require_model_secret_boundary(session: ComposeSession) -> None:
    """叠加模型 override 后只检查容器元数据，绝不打开配置文件。

    检查的事实从"Gemini secret 只挂在 worker 上"换成了"配置目录按各进程的角色
    挂载"——Provider 凭据不再是 Docker secret，而是那个目录里的一份明文文档。
    要守住的边界没变：``api`` / ``migrate`` / ``postgres`` 根本拿不到它，消费进程
    只读，只有写配置的 ``web-app`` 可写。
    """
    if session.config_source is None or not session.config_source.is_absolute():
        raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
    expected_source = os.path.normcase(os.path.normpath(session.config_source))
    model_files = (*session.files, _ROOT / "docker-compose.model.yml")
    if session.files and session.files[-1].name == "compose-smoke-inputs.json":
        model_files = (
            *session.files[:-1],
            _ROOT / "docker-compose.model.yml",
            session.files[-1],
        )
    model = session.derive(
        files=model_files,
        failure_code="SMOKE_MODEL_SECRET_COMMAND_FAILED",
    )
    model.run(
        "create",
        "--force-recreate",
        *_MODEL_AUDIT_SERVICES,
        timeout=60.0,
    )
    result = model.run("ps", "--all", "--quiet", timeout=15.0)
    container_ids = tuple(line for line in result.stdout.splitlines() if line)
    if not container_ids or len(set(container_ids)) != len(container_ids):
        raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")

    service_counts: dict[str, int] = {}
    for container_id in container_ids:
        inspected = model.run_docker(
            (
                model.docker,
                "inspect",
                "--format",
                "[{{json .Config.Labels}},{{json .Config.Env}},{{json .Mounts}}]",
                container_id,
            ),
            timeout=15.0,
            failure_code="SMOKE_MODEL_SECRET_INSPECT_FAILED",
        )
        try:
            labels, environment, mounts = json.loads(inspected.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise SmokeError("SMOKE_MODEL_SECRET_INSPECT_PROTOCOL_ERROR") from None
        if (
            not isinstance(labels, dict)
            or not isinstance(environment, list)
            or not isinstance(mounts, list)
            or not all(isinstance(item, str) for item in environment)
        ):
            raise SmokeError("SMOKE_MODEL_SECRET_INSPECT_PROTOCOL_ERROR")
        service = labels.get("com.docker.compose.service")
        if not isinstance(service, str) or not service:
            raise SmokeError("SMOKE_MODEL_SECRET_INSPECT_PROTOCOL_ERROR")
        if service not in _MODEL_AUDIT_SERVICES:
            raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        service_counts[service] = service_counts.get(service, 0) + 1
        if any(
            item.startswith(
                ("GEMINI_API_" + "KEY=", f"{_GEMINI_HOST_SECRET_FILE_ENV}=")
            )
            for item in environment
        ):
            raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        if any(
            value in item
            for value in model.sensitive_values
            for item in environment
        ):
            raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        config_mounts = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Destination") == _CONFIG_DESTINATION
        ]
        postgres_mounts = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Destination") == _POSTGRES_SECRET_DESTINATION
        ]
        enabled = [
            item
            for item in environment
            if item.startswith("XIAOWEI_GEMINI_ENABLED=")
        ]
        source = config_mounts[0].get("Source") if len(config_mounts) == 1 else None
        source_matches = isinstance(source, str) and (
            os.path.normcase(os.path.normpath(source)) == expected_source
        )
        if service == "worker":
            if (
                enabled != ["XIAOWEI_GEMINI_ENABLED=true"]
                or not source_matches
                or config_mounts[0].get("RW") is not False
                or len(postgres_mounts) != 1
                or postgres_mounts[0].get("RW") is not False
            ):
                raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        elif enabled:
            # 只有 worker 装配 Gemini；别的进程带上这个开关就是装配面漏了。
            raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        elif service == "web-app":
            # 唯一可写的那一个：配置由管理面写入。
            if not source_matches or config_mounts[0].get("RW") is not True:
                raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        elif service in _CONFIG_READ_ONLY_SERVICES:
            if not source_matches or config_mounts[0].get("RW") is not False:
                raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
        elif config_mounts:
            # api / migrate / postgres 根本不需要 Provider 凭据。
            raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")
    if set(service_counts) != set(_MODEL_AUDIT_SERVICES) or any(
        count != 1
        for service, count in service_counts.items()
        if service != "worker"
    ):
        raise SmokeError("SMOKE_MODEL_SECRET_BOUNDARY_FAILED")


def _require_web_container_boundary(session: ComposeSession) -> None:
    container = _container_id(session, "web-app")
    result = session.run_docker(
        (
            session.docker,
            "inspect",
            "--format",
            "[{{json .Config.User}},{{json .Config.Env}},"
            "{{json .HostConfig.ReadonlyRootfs}},{{json .Mounts}}]",
            container,
        ),
        timeout=15.0,
        failure_code="SMOKE_WEB_INSPECT_FAILED",
    )
    try:
        values = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError):
        raise SmokeError("SMOKE_WEB_INSPECT_PROTOCOL_ERROR") from None
    if not isinstance(values, list) or len(values) != 4:
        raise SmokeError("SMOKE_WEB_INSPECT_PROTOCOL_ERROR")
    user, environment, read_only, mounts = values
    if not isinstance(user, str) or not isinstance(environment, list) or not all(
        isinstance(item, str) for item in environment
    ) or not isinstance(mounts, list):
        raise SmokeError("SMOKE_WEB_INSPECT_PROTOCOL_ERROR")
    user_name = user.partition(":")[0].lower()
    numeric_user = user_name.lstrip("+-")
    is_root = user_name == "root" or (
        numeric_user.isdecimal() and int(user_name) == 0
    )
    required_environment = {
        "XIAOWEI_FEISHU_IDENTITY_FILE=/run/config/feishu-identities.json",
    }
    # 飞书 App Secret 不再有自己的挂载点：它在 /run/xiaowei-config/integrations.json
    # 里，而那份目录的读写属性由 _require_model_secret_boundary 逐服务核对。这里只
    # 保留身份目录——它仍然是一份独立的、必须只读的输入。
    required_mounts = {
        "/run/config/feishu-identities.json",
    }
    reference_boundary_ok = all(
        expected in environment
        and sum(item.startswith(f"{expected.partition('=')[0]}=") for item in environment)
        == 1
        for expected in required_environment
    )
    matches_by_destination = [
        [
            mount
            for mount in mounts
            if isinstance(mount, dict) and mount.get("Destination") == destination
        ]
        for destination in required_mounts
    ]
    mount_boundary_ok = all(
        len(matches) == 1 and matches[0].get("RW") is False
        for matches in matches_by_destination
    )
    if (
        not user_name
        or is_root
        or read_only is not True
        or not mount_boundary_ok
        or not reference_boundary_ok
        or any(
            value and value in item
            for value in session.sensitive_values
            for item in environment
        )
    ):
        raise SmokeError("SMOKE_WEB_CONTAINER_BOUNDARY_INVALID")
    result = session.run(
        "exec",
        "-T",
        "web-app",
        "python",
        "-c",
        _EUID_PROBE_CODE,
        timeout=15.0,
        failure_code="SMOKE_WEB_EUID_COMMAND_FAILED",
    )
    euid = result.stdout
    if (
        not isinstance(euid, str)
        or not euid
        or any(character < "0" or character > "9" for character in euid)
    ):
        raise SmokeError("SMOKE_WEB_EUID_PROTOCOL_ERROR")
    if not euid.strip("0"):
        raise SmokeError("SMOKE_WEB_CONTAINER_BOUNDARY_INVALID")


@dataclass(frozen=True)
class _WebProbeResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _request_web_oauth_start(*, host: str) -> _WebProbeResponse:
    """只直连本机固定路径；不读取代理，也不具备跟随重定向的能力。"""
    connection: http.client.HTTPConnection | None = None
    response_handle: http.client.HTTPResponse | None = None
    failed = False
    status: object = None
    headers: object = None
    body: object = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=2.0)
        connection.request("GET", _WEB_OAUTH_START_PATH, headers={"Host": host})
        response_handle = connection.getresponse()
        status = response_handle.status
        headers = response_handle.getheaders()
        body = response_handle.read(_MAX_WEB_RESPONSE_BYTES + 1)
    except Exception:
        failed = True
    finally:
        if response_handle is not None:
            try:
                response_handle.close()
            except Exception:
                failed = True
        if connection is not None:
            try:
                connection.close()
            except Exception:
                failed = True
    if (
        failed
        or type(status) is not int
        or not isinstance(headers, list | tuple)
        or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(value, str) for value in item)
            for item in headers
        )
        or not isinstance(body, bytes)
        or len(body) > _MAX_WEB_RESPONSE_BYTES
    ):
        raise SmokeError("SMOKE_WEB_OAUTH_START_REQUEST_FAILED")
    return _WebProbeResponse(
        status=status,
        headers=tuple(headers),
        body=body,
    )


def _header_values(response: _WebProbeResponse, name: str) -> list[str]:
    return [
        value
        for candidate, value in response.headers
        if candidate.lower() == name
    ]


def _trusted_oauth_state(response: _WebProbeResponse) -> str:
    locations = _header_values(response, "location")
    cookies = _header_values(response, "set-cookie")
    if response.status != 302 or len(locations) != 1 or len(cookies) != 1:
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID")
    location = locations[0]
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in location):
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID")
    try:
        parsed = urlsplit(location)
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=4,
        )
    except (TypeError, ValueError):
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID") from None
    state_values = query.get("state", [])
    if (
        f"{parsed.scheme}://{parsed.netloc}" != FEISHU_PROVIDER_ORIGIN
        or parsed.path != _WEB_AUTHORIZATION_PATH
        or parsed.fragment
        or set(query) != {"app_id", "redirect_uri", "state"}
        or query.get("app_id") != [_WEB_APP_ID]
        or query.get("redirect_uri") != [_WEB_CALLBACK_URL]
        or len(state_values) != 1
        or _OAUTH_STATE_RE.fullmatch(state_values[0]) is None
    ):
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID")
    cookie_header = cookies[0]
    if len(_OAUTH_STATE_COOKIE_PAIR_RE.findall(cookie_header)) != 1:
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID")
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except CookieError:
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID") from None
    if set(cookie) != {_OAUTH_STATE_COOKIE_NAME}:
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID")
    morsel = cookie[_OAUTH_STATE_COOKIE_NAME]
    if (
        morsel.value != state_values[0]
        or morsel["secure"] is not True
        or morsel["httponly"] is not True
        or morsel["samesite"].lower() != "lax"
        or morsel["path"] != "/"
        or morsel["domain"]
        or morsel["max-age"] != "300"
    ):
        raise SmokeError("SMOKE_WEB_OAUTH_START_INVALID")
    return state_values[0]


def _require_web_oauth_start() -> str:
    state = _trusted_oauth_state(
        _request_web_oauth_start(host=_WEB_PUBLIC_HOST)
    )
    direct = _request_web_oauth_start(host=_WEB_DIRECT_HOST)
    if (
        direct.status != 403
        or direct.body != _WEB_FORBIDDEN_BODY
        or _header_values(direct, "location")
        or _header_values(direct, "set-cookie")
    ):
        raise SmokeError("SMOKE_WEB_HOST_BOUNDARY_INVALID")
    return state


def _start_web(session: ComposeSession) -> str:
    """启动 Web，检查容器边界并走不调用 provider 的 OAuth start。"""
    session.failure_code = "SMOKE_WEB_COMMAND_FAILED"
    session.run(
        "up",
        "-d",
        "--wait",
        "--pull",
        "never",
        "--no-deps",
        "web-app",
        timeout=120.0,
    )
    _wait_ready(web=True, timeout=60.0)
    _require_web_container_boundary(session)
    return _require_web_oauth_start()


def _require_worker_scale(session: ComposeSession) -> None:
    worker_ids = session.run(
        "ps", "--quiet", "worker", timeout=15.0
    ).stdout.splitlines()
    if len(worker_ids) != 2 or len(set(worker_ids)) != 2:
        raise SmokeError("SMOKE_WORKER_SCALE_INVALID")


def _psql(session: ComposeSession, task_id: str, statement: str) -> str:
    try:
        canonical_task_id = str(uuid.UUID(task_id))
    except (ValueError, AttributeError):
        raise SmokeError("SMOKE_TASK_ID_INVALID") from None
    if canonical_task_id != task_id:
        raise SmokeError("SMOKE_TASK_ID_INVALID")
    marker = ":'task_id'"
    if marker not in statement:
        raise SmokeError("SMOKE_OBSERVATION_QUERY_INVALID")
    statement = statement.replace(marker, f"'{canonical_task_id}'")
    return session.run(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "xiaowei",
        "-d",
        "xiaowei",
        "-At",
        "-c",
        statement,
        timeout=30.0,
        failure_code="SMOKE_POSTGRES_OBSERVATION_FAILED",
    ).stdout.strip()


_NORMALISED_EVIDENCE_SQL = """
SELECT coalesce(jsonb_agg(
  jsonb_build_object(
    'step', split_part(evidence_id, ':', 2),
    'body', jsonb_set(
      envelope - 'evidence_id' - 'captured_at',
      '{limitations}', (envelope->'limitations') - 0
    )
  ) ORDER BY seq
), '[]'::jsonb)::text
FROM task_evidence WHERE task_id = :'task_id'
"""


def _normalised_evidence(session: ComposeSession, task_id: str) -> object:
    raw = _psql(session, task_id, _NORMALISED_EVIDENCE_SQL)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SmokeError("SMOKE_EVIDENCE_PROTOCOL_ERROR") from None


def _require_prometheus_persistence(
    session: ComposeSession, task_id: str
) -> None:
    observation = _psql(
        session,
        task_id,
        "SELECT coalesce((SELECT plan->>'capability_id' FROM task_plans "
        "WHERE task_id = :'task_id'), '') || '|' || "
        "(SELECT count(*)::text FROM task_evidence "
        "WHERE task_id = :'task_id')",
    )
    if observation != "prometheus.alert.evidence|2":
        raise SmokeError("SMOKE_PROMETHEUS_PERSISTENCE_MISMATCH")


def _require_same_prometheus_render(
    before: dict[str, object], after: dict[str, object]
) -> None:
    if (
        before.get("status") != "succeeded"
        or after.get("status") != "succeeded"
        or not isinstance(before.get("render"), dict)
        or before.get("render") != after.get("render")
    ):
        raise SmokeError("SMOKE_PROMETHEUS_RENDER_MISMATCH")


def _require_asset_persistence(session: ComposeSession, task_id: str) -> None:
    observation = _psql(
        session,
        task_id,
        "SELECT coalesce((SELECT plan->>'capability_id' FROM task_plans "
        "WHERE task_id = :'task_id'), '') || '|' || "
        "(SELECT count(*)::text FROM task_evidence "
        "WHERE task_id = :'task_id')",
    )
    if observation != "asset.inventory.lookup|1":
        raise SmokeError("SMOKE_ASSET_PERSISTENCE_MISMATCH")


def _require_same_asset_render(
    before: dict[str, object], after: dict[str, object]
) -> None:
    if (
        before.get("status") != "succeeded"
        or after.get("status") != "succeeded"
        or not isinstance(before.get("render"), dict)
        or before.get("render") != after.get("render")
    ):
        raise SmokeError("SMOKE_ASSET_RENDER_MISMATCH")


def _submit(session: ComposeSession, *, key: str, text: str = _TEXT) -> str:
    return _task_id(
        _task(
            session,
            "task",
            "submit",
            "--text",
            text,
            "--idempotency-key",
            key,
            failure_code="SMOKE_CLI_SUBMIT_FAILED",
        )
    )


def _migration_failure_code(logs: str) -> str:
    """把迁移容器日志收敛成固定码；任何未识别文本都不向外回显。"""
    for marker, code in _MIGRATION_FAILURE_CODES:
        if marker in logs:
            return code
    return "SMOKE_MIGRATION_FAILED"


def _require_disabled_channel_entrypoints(session: ComposeSession) -> None:
    """从已构建镜像运行三条入口；离线基线只能静默返回 disabled(2)。"""
    for module in _DISABLED_CHANNEL_ENTRYPOINTS:
        argv = session.argv("exec", "-T", "api", "python", "-m", module)
        returncode: int
        stdout: object
        stderr: object
        try:
            result = session.runner(argv, timeout=60.0)
        except subprocess.CalledProcessError as exc:
            returncode = exc.returncode
            stdout = exc.stdout
            stderr = exc.stderr
        except (OSError, subprocess.SubprocessError):
            raise SmokeError("SMOKE_CHANNEL_ENTRYPOINT_NOT_DISABLED") from None
        else:
            returncode = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        if returncode != 2 or stdout not in (None, "", b"") or stderr not in (
            None,
            "",
            b"",
        ):
            raise SmokeError("SMOKE_CHANNEL_ENTRYPOINT_NOT_DISABLED")


def _require_logs_clean(session: ComposeSession, *additional: str) -> None:
    result = session.run(
        "logs",
        "--no-color",
        "migrate",
        "api",
        "worker",
        "postgres",
        "web-app",
        timeout=30.0,
    )
    logs = f"{result.stdout}{result.stderr}"
    if any(
        value and value in logs
        for value in (*session.sensitive_values, *additional)
    ):
        raise SmokeError("SMOKE_LOG_REDACTION_FAILED")


def _run_barrier_recovery_start(session: ComposeSession) -> ComposeSession:
    """用同一已解析 Compose CLI 派生并启动 barrier worker。"""
    barrier = session.derive(
        files=(*session.files, _ROOT / "docker-compose.barrier.yml"),
        failure_code="SMOKE_BARRIER_COMMAND_FAILED",
    )
    barrier.run(
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "worker",
        timeout=60.0,
    )
    return barrier


def _full_workflow(session: ComposeSession) -> None:
    sensitive_canary = "token" + "=" + secrets.token_urlsafe(24)
    session.failure_code = "SMOKE_BUILD_COMMAND_FAILED"
    session.run("build", timeout=300.0)
    session.failure_code = "SMOKE_POSTGRES_COMMAND_FAILED"
    session.run("up", "-d", "--wait", "postgres", timeout=120.0)
    session.failure_code = "SMOKE_MIGRATION_COMMAND_FAILED"
    session.run("up", "--no-deps", "migrate", timeout=120.0)
    migrate_id = _container_id(session, "migrate", include_stopped=True)
    exit_code = session.run_docker(
        (
            session.docker,
            "inspect",
            "--format",
            "{{.State.ExitCode}}",
            migrate_id,
        ),
        timeout=15.0,
    ).stdout.strip()
    if exit_code != "0":
        logs = session.run("logs", "--no-color", "migrate", timeout=30.0).stdout
        raise SmokeError(_migration_failure_code(logs))
    session.failure_code = "SMOKE_API_COMMAND_FAILED"
    session.run("up", "-d", "--wait", "--no-deps", "api", timeout=120.0)
    _wait_ready(timeout=60.0)
    _require_disabled_channel_entrypoints(session)
    oauth_state = _start_web(session)

    session.failure_code = "SMOKE_BASELINE_COMMAND_FAILED"
    session.run(
        "up",
        "-d",
        "--no-deps",
        "worker",
        timeout=60.0,
        failure_code="SMOKE_BASELINE_WORKER_START_FAILED",
    )
    baseline_key = f"baseline-{uuid.uuid4().hex}"
    baseline_id = _submit(session, key=baseline_key, text=f"{_TEXT} {sensitive_canary}")
    _require_succeeded(_wait_task(session, baseline_id, timeout=120.0))
    baseline_evidence = _normalised_evidence(session, baseline_id)
    session.run(
        "stop",
        "worker",
        timeout=30.0,
        failure_code="SMOKE_BASELINE_WORKER_STOP_FAILED",
    )

    barrier = _run_barrier_recovery_start(session)
    if "XIAOWEI_SMOKE_STEP_BARRIER=true" not in _container_env(barrier, "worker"):
        raise SmokeError("SMOKE_BARRIER_NOT_ENABLED")
    session.failure_code = "SMOKE_BARRIER_COMMAND_FAILED"
    interrupted_id = _submit(session, key=f"interrupted-{uuid.uuid4().hex}")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        logs = session.run("logs", "--no-color", "worker", timeout=15.0).stdout
        if "XIAOWEI_SMOKE_TOOL_RESULT_READY" in logs:
            break
        time.sleep(0.5)
    else:
        raise SmokeError("SMOKE_BARRIER_MARKER_TIMEOUT")
    probe = _psql(
        session,
        interrupted_id,
        "SELECT count(*) FILTER (WHERE result_status IS NULL), "
        "(SELECT count(*) FROM task_evidence WHERE task_id = :'task_id') "
        "FROM task_step_executions WHERE task_id = :'task_id'",
    )
    if probe != "1|0":
        raise SmokeError("SMOKE_BARRIER_POSITION_INVALID")
    session.run("kill", "worker", timeout=30.0)

    session.failure_code = "SMOKE_RECOVERY_COMMAND_FAILED"
    session.run(
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "worker",
        timeout=60.0,
    )
    if any(
        item == "XIAOWEI_SMOKE_STEP_BARRIER=true"
        for item in _container_env(session, "worker")
    ):
        raise SmokeError("SMOKE_BARRIER_STILL_ENABLED")
    _require_succeeded(_wait_task(session, interrupted_id, timeout=180.0))
    if _normalised_evidence(session, interrupted_id) != baseline_evidence:
        raise SmokeError("SMOKE_RECOVERY_EVIDENCE_MISMATCH")
    attempts = _psql(
        session,
        interrupted_id,
        "SELECT max(attempt_count), "
        "(SELECT attempt_number FROM tasks WHERE task_id = :'task_id') "
        "FROM task_step_executions WHERE task_id = :'task_id'",
    )
    if attempts != "2|2":
        raise SmokeError("SMOKE_RECOVERY_ATTEMPT_MISMATCH")

    session.failure_code = "SMOKE_CONCURRENCY_COMMAND_FAILED"
    session.run("up", "-d", "--scale", "worker=2", timeout=60.0)
    _require_worker_scale(session)
    if (
        _submit(session, key=baseline_key, text=f"{_TEXT} {sensitive_canary}")
        != baseline_id
    ):
        raise SmokeError("SMOKE_IDEMPOTENCY_MISMATCH")
    keys = [f"concurrent-{uuid.uuid4().hex}" for _ in range(4)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        task_ids = list(pool.map(lambda key: _submit(session, key=key), keys))
    for task_id in task_ids:
        _require_succeeded(_wait_task(session, task_id, timeout=120.0))
        if _normalised_evidence(session, task_id) != baseline_evidence:
            raise SmokeError("SMOKE_CONCURRENT_EVIDENCE_MISMATCH")
        execution_shape = _psql(
            session,
            task_id,
            "SELECT count(*) = count(*) FILTER (WHERE attempt_count = 1"
            " AND result_status IS NOT NULL), "
            "count(*) = (SELECT count(*) FROM task_evidence "
            "WHERE task_id = :'task_id'), "
            "(SELECT attempt_number = 1 FROM tasks WHERE task_id = :'task_id') "
            "FROM task_step_executions WHERE task_id = :'task_id'",
        )
        if execution_shape != "t|t|t":
            raise SmokeError("SMOKE_CONCURRENT_EXECUTION_MISMATCH")

    session.failure_code = "SMOKE_PROMETHEUS_COMMAND_FAILED"
    prometheus_id = _submit(
        session,
        key=f"prometheus-{uuid.uuid4().hex}",
        text=_PROMETHEUS_TEXT,
    )
    prometheus_before = _wait_task(session, prometheus_id, timeout=120.0)
    _require_succeeded(prometheus_before)
    _require_prometheus_persistence(session, prometheus_id)
    session.run(
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "api",
        timeout=60.0,
        failure_code="SMOKE_PROMETHEUS_API_RESTART_FAILED",
    )
    _wait_ready(timeout=60.0)
    prometheus_after = _task(
        session,
        "task",
        "get",
        prometheus_id,
        failure_code="SMOKE_PROMETHEUS_QUERY_FAILED",
    )
    _require_same_prometheus_render(prometheus_before, prometheus_after)

    session.failure_code = "SMOKE_ASSET_COMMAND_FAILED"
    asset_id = _submit(
        session,
        key=f"asset-{uuid.uuid4().hex}",
        text=_ASSET_TEXT,
    )
    asset_before = _wait_task(session, asset_id, timeout=120.0)
    _require_succeeded(asset_before)
    _require_asset_persistence(session, asset_id)
    session.run(
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "api",
        timeout=60.0,
        failure_code="SMOKE_ASSET_API_RESTART_FAILED",
    )
    _wait_ready(timeout=60.0)
    asset_after = _task(
        session,
        "task",
        "get",
        asset_id,
        failure_code="SMOKE_ASSET_QUERY_FAILED",
    )
    _require_same_asset_render(asset_before, asset_after)

    session.failure_code = "SMOKE_FINAL_AUDIT_COMMAND_FAILED"
    console_id = _submit(session, key=f"console-{uuid.uuid4().hex}")
    _task(session, "task", "get", console_id)
    session.run("ps", "-a", timeout=15.0)
    _require_logs_clean(session, sensitive_canary, oauth_state)
    _require_model_secret_boundary(session)


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        sys.stderr.write("compose-smoke: docker_not_found\n")
        return 1
    try:
        compose_command = _resolve_compose_command(
            docker=docker, runner=_default_runner
        )
        run_smoke(
            docker=docker,
            compose_command=compose_command,
            workflow=_full_workflow,
        )
    except SmokeError as exc:
        sys.stderr.write(f"compose-smoke: {exc}\n")
        return 1
    except (OSError, subprocess.SubprocessError):
        sys.stderr.write("compose-smoke: failed\n")
        return 1
    sys.stdout.write("compose-smoke: passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

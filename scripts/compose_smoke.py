"""API/Worker/PostgreSQL fake 闭环与 M7 渠道进程离线装配验收。"""

from __future__ import annotations

import concurrent.futures
import json
import os
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
from pathlib import Path
from typing import Protocol

_ROOT = Path(__file__).resolve().parents[1]
_POSTGRES_SECRET_PATH = _ROOT / ".secrets/postgres_password"
_FEISHU_SECRET_PATH = _ROOT / ".secrets/feishu_app_secret"
_FEISHU_IDENTITY_PATH = _ROOT / ".secrets/feishu-identities.json"
_API_READY_URL = "http://127.0.0.1:8000/readyz"
_WEB_READY_URL = "http://127.0.0.1:8080/readyz"
_COMMAND_TIMEOUT = 180.0
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


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    argv: Sequence[str], *, timeout: float
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
    candidates = [(docker, "compose")]
    standalone = shutil.which("docker-compose")
    if standalone is not None:
        candidates.append((standalone,))
    for command in candidates:
        try:
            runner((*command, "version"), timeout=15.0)
        except (OSError, subprocess.SubprocessError):
            continue
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
    parent_device: int
    parent_inode: int
    device: int
    inode: int


def _create_input(path: Path, content: str) -> _OwnedInput:
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
    except FileExistsError:
        os.close(directory)
        raise SmokeError("SMOKE_INPUT_ALREADY_EXISTS") from None
    except OSError:
        os.close(directory)
        raise SmokeError("SMOKE_INPUT_CREATE_FAILED") from None
    failed = False
    file_stat: os.stat_result | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            file_stat = os.fstat(stream.fileno())
    except (OSError, UnicodeError):
        failed = True
    if failed:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(path.name, dir_fd=directory)
        except OSError:
            pass
        os.close(directory)
        raise SmokeError("SMOKE_INPUT_CREATE_FAILED") from None
    os.close(directory)
    if file_stat is None:
        raise RuntimeError("missing created input identity")
    return _OwnedInput(
        path=path,
        parent_device=parent_stat.st_dev,
        parent_inode=parent_stat.st_ino,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
    )


def _create_secret(path: Path) -> tuple[str, _OwnedInput]:
    value = secrets.token_urlsafe(32)
    owned = _create_input(path, f"{value}\n")
    return value, owned


def _remove_owned_inputs(inputs: Sequence[_OwnedInput]) -> None:
    failed = False
    for owned in inputs:
        path = owned.path
        try:
            directory = os.open(
                path.parent,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            failed = True
            continue
        try:
            parent_stat = os.fstat(directory)
            if (parent_stat.st_dev, parent_stat.st_ino) != (
                owned.parent_device,
                owned.parent_inode,
            ):
                failed = True
                continue
            file_stat = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (file_stat.st_dev, file_stat.st_ino) != (owned.device, owned.inode):
                failed = True
                continue
            os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            failed = True
        finally:
            os.close(directory)
    if failed:
        raise SmokeError("SMOKE_INPUT_CLEANUP_FAILED") from None


def _create_smoke_inputs(
    *, postgres_secret: Path, feishu_secret: Path, identity: Path
) -> tuple[tuple[str, ...], tuple[_OwnedInput, ...]]:
    created: list[_OwnedInput] = []
    try:
        postgres_value, postgres_owned = _create_secret(postgres_secret)
        created.append(postgres_owned)
        feishu_value, feishu_owned = _create_secret(feishu_secret)
        created.append(feishu_owned)
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
    except SmokeError:
        _remove_owned_inputs(created)
        raise
    return (postgres_value, feishu_value), tuple(created)


@dataclass
class ComposeSession:
    docker: str
    runner: CommandRunner
    project: str
    files: tuple[Path, ...]
    compose_command: tuple[str, ...]
    sensitive_values: tuple[str, ...] = ()
    up_started: bool = False
    failure_code: str = "SMOKE_COMPOSE_COMMAND_FAILED"

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
            up_started=self.up_started,
            failure_code=failure_code,
        )

    def run(
        self,
        *arguments: str,
        timeout: float = _COMMAND_TIMEOUT,
        failure_code: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if "up" in arguments:
            self.up_started = True
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
    postgres_secret_path: Path = _POSTGRES_SECRET_PATH,
    feishu_secret_path: Path = _FEISHU_SECRET_PATH,
    identity_path: Path = _FEISHU_IDENTITY_PATH,
) -> None:
    """只清理本次通过预检且实际尝试启动的随机 Compose project。"""
    project = project_name()
    _preflight(docker=docker, runner=runner, project=project)
    sensitive_values, owned_inputs = _create_smoke_inputs(
        postgres_secret=postgres_secret_path,
        feishu_secret=feishu_secret_path,
        identity=identity_path,
    )
    session = ComposeSession(
        docker=docker,
        runner=runner,
        project=project,
        files=(_ROOT / "docker-compose.yml", _ROOT / "docker-compose.smoke.yml"),
        compose_command=compose_command,
        sensitive_values=sensitive_values,
    )
    try:
        workflow(session)
    finally:
        try:
            if session.up_started:
                session.failure_code = "SMOKE_CLEANUP_COMMAND_FAILED"
                session.run(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    timeout=60.0,
                )
        finally:
            _remove_owned_inputs(owned_inputs)


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


def _wait_ready(*, web: bool = False, timeout: float) -> None:
    url = _WEB_READY_URL if web else _API_READY_URL
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 -- URL 来自上述闭集
                url, timeout=2.0
            ) as response:
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
        "XIAOWEI_FEISHU_APP_SECRET_FILE=/run/secrets/feishu_app_secret",
        "XIAOWEI_FEISHU_IDENTITY_FILE=/run/config/feishu-identities.json",
    }
    required_mounts = {
        "/run/secrets/feishu_app_secret",
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


def _start_web(session: ComposeSession) -> None:
    """只启动 Web 并检查健康、ready 与容器边界，不触发 OAuth 路由。"""
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
    _start_web(session)

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

    barrier_files = (*session.files, _ROOT / "docker-compose.barrier.yml")
    barrier = session.derive(
        files=barrier_files,
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
    _require_logs_clean(session, sensitive_canary)


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

"""M5 API/Worker/PostgreSQL Compose 的可恢复 fake 闭环验收。"""

from __future__ import annotations

import concurrent.futures
import json
import os
import secrets
import shutil
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
_SECRET_PATH = _ROOT / ".secrets/postgres_password"
_COMMAND_TIMEOUT = 180.0
_TERMINAL = {"succeeded", "failed", "rejected", "canceled", "indeterminate"}
_TEXT = "检查最近三十分钟慢查询"


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
        result = runner(argv, timeout=15.0)
        if result.stdout.strip():
            raise SmokeError("SMOKE_PROJECT_COLLISION")


def _create_secret(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SmokeError("SMOKE_SECRET_ALREADY_EXISTS") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(32))
        stream.write("\n")


@dataclass
class ComposeSession:
    docker: str
    runner: CommandRunner
    project: str
    files: tuple[Path, ...]
    up_started: bool = False

    def argv(self, *arguments: str) -> list[str]:
        command = [self.docker, "compose", "-p", self.project]
        for path in self.files:
            command.extend(("-f", str(path)))
        command.extend(arguments)
        return command

    def run(
        self, *arguments: str, timeout: float = _COMMAND_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        if "up" in arguments:
            self.up_started = True
        return self.runner(self.argv(*arguments), timeout=timeout)


Workflow = Callable[[ComposeSession], None]


def run_smoke(
    *,
    docker: str,
    runner: CommandRunner = _default_runner,
    workflow: Workflow,
    secret_path: Path = _SECRET_PATH,
) -> None:
    """只清理本次通过预检且实际尝试启动的随机 Compose project。"""
    project = project_name()
    _preflight(docker=docker, runner=runner, project=project)
    _create_secret(secret_path)
    session = ComposeSession(
        docker=docker,
        runner=runner,
        project=project,
        files=(_ROOT / "docker-compose.yml", _ROOT / "docker-compose.smoke.yml"),
    )
    try:
        workflow(session)
    finally:
        try:
            if session.up_started:
                session.run(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    timeout=60.0,
                )
        finally:
            secret_path.unlink(missing_ok=True)


def _task(session: ComposeSession, *arguments: str) -> dict[str, object]:
    result = session.run("exec", "-T", "api", "xiaowei", *arguments, timeout=30.0)
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
        value = _task(session, "task", "get", task_id)
        if value.get("status") in _TERMINAL:
            return value
        time.sleep(0.5)
    raise SmokeError("SMOKE_TASK_TIMEOUT")


def _require_succeeded(value: dict[str, object]) -> None:
    if value.get("status") != "succeeded":
        raise SmokeError("SMOKE_TASK_NOT_SUCCEEDED")


def _wait_ready(*, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8000/readyz", timeout=2.0
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
    result = session.runner(
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


def _require_worker_scale(session: ComposeSession) -> None:
    worker_ids = session.run(
        "ps", "--quiet", "worker", timeout=15.0
    ).stdout.splitlines()
    if len(worker_ids) != 2 or len(set(worker_ids)) != 2:
        raise SmokeError("SMOKE_WORKER_SCALE_INVALID")


def _psql(session: ComposeSession, task_id: str, statement: str) -> str:
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
        "-v",
        f"task_id={task_id}",
        "-c",
        statement,
        timeout=30.0,
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
        )
    )


def _full_workflow(session: ComposeSession) -> None:
    sensitive_canary = "token" + "=" + secrets.token_urlsafe(24)
    session.run("build", timeout=300.0)
    session.run("up", "-d", "--wait", "postgres", timeout=120.0)
    session.run("up", "--no-deps", "migrate", timeout=120.0)
    migrate_id = _container_id(session, "migrate", include_stopped=True)
    exit_code = session.runner(
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
        raise SmokeError("SMOKE_MIGRATION_FAILED")
    session.run("up", "-d", "--wait", "--no-deps", "api", timeout=120.0)
    _wait_ready(timeout=60.0)

    session.run("up", "-d", "--no-deps", "worker", timeout=60.0)
    baseline_key = f"baseline-{uuid.uuid4().hex}"
    baseline_id = _submit(session, key=baseline_key, text=f"{_TEXT} {sensitive_canary}")
    _require_succeeded(_wait_task(session, baseline_id, timeout=120.0))
    baseline_evidence = _normalised_evidence(session, baseline_id)
    session.run("stop", "worker", timeout=30.0)

    barrier_files = (*session.files, _ROOT / "docker-compose.barrier.yml")
    barrier = ComposeSession(
        docker=session.docker,
        runner=session.runner,
        project=session.project,
        files=barrier_files,
        up_started=True,
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

    console_id = _submit(session, key=f"console-{uuid.uuid4().hex}")
    _task(session, "task", "get", console_id)
    session.run("ps", "-a", timeout=15.0)
    logs = "".join(
        session.run("logs", "--no-color", service, timeout=30.0).stdout
        for service in ("migrate", "api", "worker", "postgres")
    )
    if sensitive_canary in logs:
        raise SmokeError("SMOKE_LOG_REDACTION_FAILED")


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        sys.stderr.write("compose-smoke: docker_not_found\n")
        return 1
    try:
        run_smoke(docker=docker, workflow=_full_workflow)
    except (SmokeError, OSError, subprocess.SubprocessError):
        sys.stderr.write("compose-smoke: failed\n")
        return 1
    sys.stdout.write("compose-smoke: passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

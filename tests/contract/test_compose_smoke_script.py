"""smoke 资源归属、超时与清理命令的反例。"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import compose_smoke
from scripts.compose_smoke import ComposeSession, SmokeError, project_name, run_smoke


class RecordingRunner:
    def __init__(self, *, collision_at: int | None = None) -> None:
        self.collision_at = collision_at
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Any, *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert timeout > 0
        call = tuple(argv)
        self.calls.append(call)
        output = "existing-resource\n" if len(self.calls) == self.collision_at else ""
        return subprocess.CompletedProcess(call, 0, stdout=output, stderr="")


def _mutating_commands(calls: list[tuple[str, ...]]) -> set[str]:
    return {token for call in calls for token in call if token in {"up", "down", "rm"}}


@pytest.mark.parametrize("collision_at", [1, 2, 3])
def test_collision_preflight_never_mutates_or_cleans_existing_project(
    collision_at: int, tmp_path: Path
) -> None:
    runner = RecordingRunner(collision_at=collision_at)
    workflow_called = False

    def workflow(_: ComposeSession) -> None:
        nonlocal workflow_called
        workflow_called = True

    with pytest.raises(SmokeError, match="SMOKE_PROJECT_COLLISION"):
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker", "compose"),
            runner=runner,
            workflow=workflow,
            postgres_secret_path=tmp_path / ".secrets/postgres_password",
            feishu_secret_path=tmp_path / ".secrets/feishu_app_secret",
            identity_path=tmp_path / ".secrets/feishu-identities.json",
        )
    assert workflow_called is False
    assert not _mutating_commands(runner.calls)


def test_up_failure_still_cleans_only_the_generated_project(tmp_path: Path) -> None:
    class FailingRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if "up" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return result

    runner = FailingRunner()

    def workflow(session: ComposeSession) -> None:
        session.run("up", "-d", "postgres")

    with pytest.raises(SmokeError, match="SMOKE_COMPOSE_COMMAND_FAILED"):
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker", "compose"),
            runner=runner,
            workflow=workflow,
            postgres_secret_path=tmp_path / ".secrets/postgres_password",
            feishu_secret_path=tmp_path / ".secrets/feishu_app_secret",
            identity_path=tmp_path / ".secrets/feishu-identities.json",
        )
    down = [call for call in runner.calls if "down" in call]
    assert len(down) == 1
    assert down[0][:4] == (
        "/usr/bin/docker",
        "compose",
        "--profile",
        "m7-channels",
    )
    assert "down" in down[0]
    assert "--volumes" in down[0]
    assert "--remove-orphans" in down[0]
    assert not any(
        (tmp_path / ".secrets" / name).exists()
        for name in (
            "postgres_password",
            "feishu_app_secret",
            "feishu-identities.json",
        )
    )


def test_project_names_have_a_full_random_uuid_suffix() -> None:
    first = project_name()
    second = project_name()
    assert first != second
    assert re.fullmatch(r"xiaowei_m5_smoke_[0-9a-f]{32}", first)


def test_compose_session_uses_one_resolved_command_for_derived_sessions() -> None:
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=RecordingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker-compose",),
    )

    derived = session.derive(
        files=(*session.files, Path("docker-compose.barrier.yml")),
        failure_code="SMOKE_BARRIER_COMMAND_FAILED",
    )

    assert session.argv("config")[:5] == [
        "/usr/bin/docker-compose",
        "--profile",
        "m7-channels",
        "-p",
        "isolated",
    ]
    assert derived.argv("config")[:5] == session.argv("config")[:5]
    assert derived.compose_command == session.compose_command
    assert derived.files[-1] == Path("docker-compose.barrier.yml")


def test_smoke_executes_each_channel_entrypoint_with_live_flags_disabled() -> None:
    class DisabledRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            raise subprocess.CalledProcessError(
                2,
                argv,
                output="",
                stderr="",
            )

    runner = DisabledRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"), Path("docker-compose.smoke.yml")),
        compose_command=("/usr/bin/docker", "compose"),
        up_started=True,
    )

    compose_smoke._require_disabled_channel_entrypoints(session)

    assert [call[-6:] for call in runner.calls] == [
        (
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "xiaowei_agent.interfaces.feishu_listener",
        ),
        (
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "xiaowei_agent.interfaces.feishu_worker",
        ),
        (
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "xiaowei_agent.interfaces.web_app",
        ),
    ]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (0, "", ""),
        (1, "", ""),
        (2, "unexpected-output", ""),
        (2, "", "unexpected-error"),
    ],
)
def test_channel_entrypoint_smoke_rejects_any_non_silent_disabled_outcome(
    returncode: int, stdout: str, stderr: str
) -> None:
    class OutcomeRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            result = subprocess.CompletedProcess(
                argv,
                returncode,
                stdout=stdout,
                stderr=stderr,
            )
            if returncode:
                raise subprocess.CalledProcessError(
                    returncode,
                    argv,
                    output=stdout,
                    stderr=stderr,
                )
            return result

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=OutcomeRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
        up_started=True,
    )

    with pytest.raises(
        SmokeError, match=r"^SMOKE_CHANNEL_ENTRYPOINT_NOT_DISABLED$"
    ) as caught:
        compose_smoke._require_disabled_channel_entrypoints(session)
    assert "unexpected" not in str(caught.value)


def test_generated_secret_is_host_isolated_and_container_readable(tmp_path: Path) -> None:
    path = tmp_path / ".secrets" / "postgres_password"
    compose_smoke._create_secret(path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_smoke_owns_and_cleans_all_three_generated_input_files(tmp_path: Path) -> None:
    parent = tmp_path / ".secrets"
    postgres = parent / "postgres_password"
    feishu = parent / "feishu_app_secret"
    identities = parent / "feishu-identities.json"
    observed_sensitive_values: tuple[str, ...] = ()

    def workflow(session: ComposeSession) -> None:
        nonlocal observed_sensitive_values
        observed_sensitive_values = session.sensitive_values
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o444
            for path in (postgres, feishu, identities)
        )
        assert json.loads(identities.read_text(encoding="utf-8")) == {
            "version": 1,
            "tenant_id": "dev-local",
            "environment_id": "dev",
            "entries": [],
        }
        assert postgres.read_text(encoding="utf-8").strip() in session.sensitive_values
        assert feishu.read_text(encoding="utf-8").strip() in session.sensitive_values

    run_smoke(
        docker="/usr/bin/docker",
        compose_command=("/usr/bin/docker-compose",),
        runner=RecordingRunner(),
        workflow=workflow,
        postgres_secret_path=postgres,
        feishu_secret_path=feishu,
        identity_path=identities,
    )

    assert len(observed_sensitive_values) == 2
    assert not any(path.exists() for path in (postgres, feishu, identities))


@pytest.mark.parametrize("existing_name", ["feishu_app_secret", "feishu-identities.json"])
def test_partial_input_creation_failure_preserves_existing_file_and_cleans_only_owned(
    tmp_path: Path, existing_name: str
) -> None:
    parent = tmp_path / ".secrets"
    parent.mkdir(mode=0o700)
    postgres = parent / "postgres_password"
    feishu = parent / "feishu_app_secret"
    identities = parent / "feishu-identities.json"
    existing = parent / existing_name
    existing.write_text("user-owned\n", encoding="utf-8")

    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_ALREADY_EXISTS$"):
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker-compose",),
            runner=RecordingRunner(),
            workflow=lambda _: pytest.fail("workflow must not run"),
            postgres_secret_path=postgres,
            feishu_secret_path=feishu,
            identity_path=identities,
        )

    assert existing.read_text(encoding="utf-8") == "user-owned\n"
    for path in (postgres, feishu, identities):
        if path != existing:
            assert not path.exists()


def test_cleanup_refuses_a_replaced_parent_directory_and_preserves_new_files(
    tmp_path: Path,
) -> None:
    parent = tmp_path / ".secrets"
    moved = tmp_path / "owned-inputs"
    postgres = parent / "postgres_password"
    feishu = parent / "feishu_app_secret"
    identities = parent / "feishu-identities.json"

    def replace_parent(_: ComposeSession) -> None:
        parent.rename(moved)
        parent.mkdir(mode=0o700)
        postgres.write_text("user-owned\n", encoding="utf-8")

    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_CLEANUP_FAILED$"):
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker", "compose"),
            runner=RecordingRunner(),
            workflow=replace_parent,
            postgres_secret_path=postgres,
            feishu_secret_path=feishu,
            identity_path=identities,
        )

    assert postgres.read_text(encoding="utf-8") == "user-owned\n"
    assert (moved / "postgres_password").exists()


def test_secret_creation_rejects_a_traversable_parent_directory(tmp_path: Path) -> None:
    parent = tmp_path / ".secrets"
    parent.mkdir(mode=0o755)
    path = parent / "postgres_password"

    with pytest.raises(SmokeError, match="SMOKE_INPUT_DIRECTORY_PERMISSIONS"):
        compose_smoke._create_secret(path)
    assert not path.exists()


def test_input_creation_rejects_a_symlink_parent_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    parent = tmp_path / "linked-secrets"
    parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_DIRECTORY_INVALID$"):
        compose_smoke._create_secret(parent / "feishu_app_secret")

    assert not (target / "feishu_app_secret").exists()


def test_input_creation_closes_the_parent_fd_when_directory_inspection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = os.fstat
    inspected_fd: int | None = None

    def fail_inspection(descriptor: int) -> os.stat_result:
        nonlocal inspected_fd
        inspected_fd = descriptor
        raise OSError("private-inspection-error")

    monkeypatch.setattr(compose_smoke.os, "fstat", fail_inspection)

    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_DIRECTORY_INVALID$"):
        compose_smoke._create_secret(tmp_path / ".secrets" / "feishu_app_secret")

    assert inspected_fd is not None
    with pytest.raises(OSError):
        real_fstat(inspected_fd)


def test_missing_docker_is_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: None)
    assert compose_smoke.main() == 1
    assert capsys.readouterr().err == "compose-smoke: docker_not_found\n"


def test_compose_plugin_is_preferred_when_its_version_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RecordingRunner()
    monkeypatch.setattr(
        compose_smoke.shutil,
        "which",
        lambda name: "/usr/bin/docker-compose" if name == "docker-compose" else None,
    )

    command = compose_smoke._resolve_compose_command(
        docker="/usr/bin/docker", runner=runner
    )

    assert command == ("/usr/bin/docker", "compose")
    assert runner.calls == [("/usr/bin/docker", "compose", "version")]


def test_compose_probe_falls_back_to_verified_standalone_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FallbackRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv) == ("/usr/bin/docker", "compose", "version"):
                raise subprocess.CalledProcessError(
                    1, argv, stderr="private-plugin-error"
                )
            return result

    runner = FallbackRunner()
    monkeypatch.setattr(
        compose_smoke.shutil,
        "which",
        lambda name: "/usr/bin/docker-compose" if name == "docker-compose" else None,
    )

    command = compose_smoke._resolve_compose_command(
        docker="/usr/bin/docker", runner=runner
    )

    assert command == ("/usr/bin/docker-compose",)
    assert runner.calls == [
        ("/usr/bin/docker", "compose", "version"),
        ("/usr/bin/docker-compose", "version"),
    ]


def test_compose_probe_double_failure_exposes_only_a_fixed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            raise subprocess.CalledProcessError(
                1, argv, stderr="private-compose-output"
            )

    monkeypatch.setattr(
        compose_smoke.shutil,
        "which",
        lambda name: "/usr/bin/docker-compose" if name == "docker-compose" else None,
    )
    runner = FailingRunner()

    with pytest.raises(SmokeError, match=r"^SMOKE_COMPOSE_NOT_FOUND$") as caught:
        compose_smoke._resolve_compose_command(
            docker="/usr/bin/docker", runner=runner
        )

    assert caught.value.__context__ is None
    assert "private-compose-output" not in str(caught.value)
    assert runner.calls == [
        ("/usr/bin/docker", "compose", "version"),
        ("/usr/bin/docker-compose", "version"),
    ]


def test_main_reports_only_the_fixed_smoke_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        compose_smoke,
        "_resolve_compose_command",
        lambda **_: ("/usr/bin/docker", "compose"),
    )
    monkeypatch.setattr(
        compose_smoke,
        "run_smoke",
        lambda **_: (_ for _ in ()).throw(SmokeError("SMOKE_BASELINE_COMMAND_FAILED")),
    )

    assert compose_smoke.main() == 1
    assert capsys.readouterr().err == "compose-smoke: SMOKE_BASELINE_COMMAND_FAILED\n"


def test_web_smoke_uses_profile_health_wait_and_only_the_readiness_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"), Path("docker-compose.smoke.yml")),
        compose_command=("/usr/bin/docker-compose",),
    )
    readiness: list[tuple[bool, float]] = []
    monkeypatch.setattr(
        compose_smoke,
        "_wait_ready",
        lambda *, web, timeout: readiness.append((web, timeout)),
    )
    monkeypatch.setattr(
        compose_smoke, "_require_web_container_boundary", lambda _: None
    )

    compose_smoke._start_web(session)

    assert runner.calls[0][:3] == (
        "/usr/bin/docker-compose",
        "--profile",
        "m7-channels",
    )
    assert runner.calls[0][-7:] == (
        "up",
        "-d",
        "--wait",
        "--pull",
        "never",
        "--no-deps",
        "web-app",
    )
    assert readiness == [(True, 60.0)]


def test_web_readiness_requests_only_the_local_readyz_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def open_url(url: str, *, timeout: float) -> Response:
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(compose_smoke.urllib.request, "urlopen", open_url)

    compose_smoke._wait_ready(web=True, timeout=0.1)

    assert calls == [("http://127.0.0.1:8080/readyz", 2.0)]


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("postgres-value-in-log", ""),
        ("", "feishu-value-in-log"),
    ],
)
def test_final_log_audit_includes_web_and_rejects_either_generated_secret(
    stdout: str, stderr: str
) -> None:
    class LogsRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            return subprocess.CompletedProcess(
                argv, 0, stdout=stdout, stderr=stderr
            )

    runner = LogsRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
        sensitive_values=("postgres-value", "feishu-value"),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_LOG_REDACTION_FAILED$"):
        compose_smoke._require_logs_clean(session, "request-canary")

    assert runner.calls[-1][-7:] == (
        "logs",
        "--no-color",
        "migrate",
        "api",
        "worker",
        "postgres",
        "web-app",
    )


def _web_inspect_payload(
    *,
    user: str = "xiaowei",
    read_only: bool = True,
    secret_writable: bool = False,
    include_secret_mount: bool = True,
    include_identity_mount: bool = True,
    duplicate_identity_mount: bool = False,
    environment: list[str] | None = None,
) -> str:
    mounts: list[dict[str, object]] = []
    if include_secret_mount:
        mounts.append(
            {
                "Destination": "/run/secrets/feishu_app_secret",
                "RW": secret_writable,
            }
        )
    if include_identity_mount:
        mounts.append(
            {
                "Destination": "/run/config/feishu-identities.json",
                "RW": False,
            }
        )
    if duplicate_identity_mount:
        mounts.append(
            {
                "Destination": "/run/config/feishu-identities.json",
                "RW": False,
            }
        )
    return json.dumps(
        [
            user,
            environment
            or [
                "XIAOWEI_FEISHU_APP_SECRET_FILE=/run/secrets/feishu_app_secret",
                "XIAOWEI_FEISHU_IDENTITY_FILE=/run/config/feishu-identities.json",
            ],
            read_only,
            mounts,
        ]
    )


def test_web_container_boundary_accepts_non_root_readonly_reference_mounts() -> None:
    class InspectRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv)[-3:] == ("ps", "--quiet", "web-app"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout="web-container\n", stderr=""
                )
            if tuple(argv)[:2] == ("/usr/bin/docker", "inspect"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=_web_inspect_payload(), stderr=""
                )
            return result

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=InspectRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
        sensitive_values=("private-fake-secret",),
    )
    compose_smoke._require_web_container_boundary(session)


@pytest.mark.parametrize(
    "payload",
    [
        _web_inspect_payload(user="0"),
        _web_inspect_payload(user="+0:1000"),
        _web_inspect_payload(read_only=False),
        _web_inspect_payload(secret_writable=True),
        _web_inspect_payload(include_secret_mount=False),
        _web_inspect_payload(include_identity_mount=False),
        _web_inspect_payload(duplicate_identity_mount=True),
        _web_inspect_payload(
            environment=[
                "XIAOWEI_FEISHU_APP_SECRET_FILE=/run/secrets/feishu_app_secret",
                "XIAOWEI_FEISHU_APP_SECRET_FILE=/tmp/alternate",
                "XIAOWEI_FEISHU_IDENTITY_FILE=/run/config/feishu-identities.json",
            ]
        ),
        _web_inspect_payload(environment=["LEAK=private-fake-secret"]),
    ],
)
def test_web_container_boundary_rejects_privilege_mount_or_secret_leak(
    payload: str,
) -> None:
    class InspectRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv)[-3:] == ("ps", "--quiet", "web-app"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout="web-container\n", stderr=""
                )
            if tuple(argv)[:2] == ("/usr/bin/docker", "inspect"):
                return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
            return result

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=InspectRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
        sensitive_values=("private-fake-secret",),
    )
    with pytest.raises(SmokeError, match=r"^SMOKE_WEB_CONTAINER_BOUNDARY_INVALID$"):
        compose_smoke._require_web_container_boundary(session)


def test_compose_command_failure_is_attributed_to_the_current_phase() -> None:
    class FailingRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            raise subprocess.CalledProcessError(1, argv, stderr="private-driver-output")

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=FailingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    session.failure_code = "SMOKE_BUILD_COMMAND_FAILED"

    with pytest.raises(SmokeError, match=r"^SMOKE_BUILD_COMMAND_FAILED$") as caught:
        session.run("build")
    assert caught.value.__context__ is None
    assert "private-driver-output" not in str(caught.value)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (lambda session: compose_smoke._submit(session, key="key"), "SMOKE_CLI_SUBMIT_FAILED"),
        (
            lambda session: compose_smoke._wait_task(session, "task", timeout=1.0),
            "SMOKE_CLI_QUERY_FAILED",
        ),
        (
            lambda session: compose_smoke._psql(
                session,
                "12345678-1234-4321-9234-123456789abc",
                "SELECT 1 WHERE :'task_id' IS NOT NULL",
            ),
            "SMOKE_POSTGRES_OBSERVATION_FAILED",
        ),
    ],
)
def test_external_action_failure_is_attributed_without_exposing_output(
    action: Any, code: str
) -> None:
    class FailingRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            raise subprocess.CalledProcessError(
                1, argv, stderr="private-external-output"
            )

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=FailingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )

    with pytest.raises(SmokeError, match=rf"^{code}$") as caught:
        action(session)
    assert caught.value.__context__ is None
    assert "private-external-output" not in str(caught.value)


def test_psql_uses_a_server_parseable_uuid_literal() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    task_id = "12345678-1234-4321-9234-123456789abc"

    compose_smoke._psql(
        session,
        task_id,
        "SELECT count(*) FROM tasks WHERE task_id = :'task_id'",
    )

    command = runner.calls[-1]
    assert "-v" not in command
    assert ":'task_id'" not in command[-1]
    assert command[-1].endswith(f"'{task_id}'")


def test_psql_rejects_a_non_uuid_task_id_before_running_docker() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_TASK_ID_INVALID$"):
        compose_smoke._psql(session, "' OR TRUE; --", "SELECT 1")
    assert not runner.calls


def test_psql_rejects_an_unscoped_observation_before_running_docker() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_OBSERVATION_QUERY_INVALID$"):
        compose_smoke._psql(
            session,
            "12345678-1234-4321-9234-123456789abc",
            "SELECT count(*) FROM tasks",
        )
    assert not runner.calls


@pytest.mark.parametrize(
    ("logs", "expected"),
    [
        ("xiaowei-migrate: configuration_error", "SMOKE_MIGRATION_CONFIGURATION_FAILED"),
        ("xiaowei-migrate: database_unavailable", "SMOKE_MIGRATION_DATABASE_UNAVAILABLE"),
        ("xiaowei-migrate: database_error", "SMOKE_MIGRATION_DATABASE_ERROR"),
        ("xiaowei-migrate: migration_command_error", "SMOKE_MIGRATION_COMMAND_ERROR"),
        ("xiaowei-migrate: io_error", "SMOKE_MIGRATION_IO_ERROR"),
        ("private unclassified output", "SMOKE_MIGRATION_FAILED"),
    ],
)
def test_migration_logs_are_reduced_to_a_fixed_smoke_code(
    logs: str, expected: str
) -> None:
    assert compose_smoke._migration_failure_code(logs) == expected


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("failed", "SMOKE_TASK_FAILED"),
        ("rejected", "SMOKE_TASK_REJECTED"),
        ("canceled", "SMOKE_TASK_CANCELED"),
        ("indeterminate", "SMOKE_TASK_INDETERMINATE"),
        ("hostile-status", "SMOKE_TASK_STATUS_INVALID"),
    ],
)
def test_non_success_task_status_is_reduced_to_a_fixed_code(
    status: str, code: str
) -> None:
    with pytest.raises(SmokeError, match=rf"^{code}$"):
        compose_smoke._require_succeeded({"status": status})


def test_exited_migration_is_looked_up_with_all_containers() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    with pytest.raises(SmokeError, match="SMOKE_CONTAINER_ID_INVALID"):
        compose_smoke._container_id(session, "migrate", include_stopped=True)
    assert runner.calls[-1][-4:] == ("ps", "--all", "--quiet", "migrate")


def test_worker_scale_requires_two_distinct_running_containers() -> None:
    class ScaleRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv)[-3:] == ("ps", "--quiet", "worker"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout="worker-a\nworker-b\n", stderr=""
                )
            return result

    runner = ScaleRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    compose_smoke._require_worker_scale(session)


@pytest.mark.parametrize("stdout", ["worker-a\n", "worker-a\nworker-a\n"])
def test_worker_scale_rejects_missing_or_duplicate_containers(stdout: str) -> None:
    class ScaleRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv)[-3:] == ("ps", "--quiet", "worker"):
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
            return result

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=ScaleRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    with pytest.raises(SmokeError, match="SMOKE_WORKER_SCALE_INVALID"):
        compose_smoke._require_worker_scale(session)


@pytest.mark.parametrize(
    ("observation", "fails"),
    [("prometheus.alert.evidence|2", False), ("prometheus.alert.evidence|1", True)],
)
def test_prometheus_smoke_requires_its_plan_and_two_persisted_evidences(
    monkeypatch: pytest.MonkeyPatch, observation: str, fails: bool
) -> None:
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=RecordingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    monkeypatch.setattr(compose_smoke, "_psql", lambda *_args, **_kwargs: observation)
    task_id = "12345678-1234-4321-9234-123456789abc"
    if fails:
        with pytest.raises(SmokeError, match="SMOKE_PROMETHEUS_PERSISTENCE_MISMATCH"):
            compose_smoke._require_prometheus_persistence(session, task_id)
    else:
        compose_smoke._require_prometheus_persistence(session, task_id)


def test_prometheus_render_must_survive_api_restart_byte_for_byte() -> None:
    before = {"status": "succeeded", "render": {"answer": "constant"}}
    compose_smoke._require_same_prometheus_render(before, dict(before))
    with pytest.raises(SmokeError, match="SMOKE_PROMETHEUS_RENDER_MISMATCH"):
        compose_smoke._require_same_prometheus_render(
            before,
            {"status": "succeeded", "render": {"answer": "changed"}},
        )


@pytest.mark.parametrize(
    ("observation", "fails"),
    [
        ("asset.inventory.lookup|1", False),
        ("asset.inventory.lookup|0", True),
        ("asset.inventory.lookup|2", True),
    ],
)
def test_asset_smoke_requires_its_plan_and_one_persisted_evidence(
    monkeypatch: pytest.MonkeyPatch, observation: str, fails: bool
) -> None:
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=RecordingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )
    monkeypatch.setattr(compose_smoke, "_psql", lambda *_args, **_kwargs: observation)
    task_id = "12345678-1234-4321-9234-123456789abc"
    if fails:
        with pytest.raises(SmokeError, match="SMOKE_ASSET_PERSISTENCE_MISMATCH"):
            compose_smoke._require_asset_persistence(session, task_id)
    else:
        compose_smoke._require_asset_persistence(session, task_id)


def test_asset_render_must_survive_api_restart_byte_for_byte() -> None:
    before = {"status": "succeeded", "render": {"answer": "constant"}}
    compose_smoke._require_same_asset_render(before, dict(before))
    with pytest.raises(SmokeError, match="SMOKE_ASSET_RENDER_MISMATCH"):
        compose_smoke._require_same_asset_render(
            before,
            {"status": "succeeded", "render": {"answer": "changed"}},
        )

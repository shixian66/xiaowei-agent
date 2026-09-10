"""smoke 资源归属、超时与清理命令的反例。"""

import json
import os
import re
import stat
import subprocess
import traceback
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.response import addinfourl

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


class CleanupBoundaryError(BaseException):
    """代表不会被 ``except Exception`` 捕获的清理失败。"""


class NoteRejectingError(BaseException):
    """模拟覆写 ``add_note`` 后拒绝记录的外部异常。"""

    def __init__(self) -> None:
        super().__init__("private-primary-detail")
        self.attempted_notes: list[str] = []

    def add_note(self, note: str) -> None:
        self.attempted_notes.append(note)
        raise RuntimeError("private-note-detail")


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
            input_root=tmp_path / ".secrets",
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
            input_root=tmp_path / ".secrets",
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


def test_successful_workflow_reports_the_fixed_down_cleanup_code(
    tmp_path: Path,
) -> None:
    class DownFailingRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if "down" in argv:
                raise subprocess.CalledProcessError(
                    1,
                    argv,
                    stderr="private-down-detail",
                )
            return result

    runner = DownFailingRunner()

    def workflow(session: ComposeSession) -> None:
        session.up_started = True

    with pytest.raises(
        SmokeError,
        match=r"^SMOKE_CLEANUP_COMMAND_FAILED$",
    ) as caught:
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker-compose",),
            runner=runner,
            workflow=workflow,
            input_root=tmp_path / ".secrets",
        )

    assert str(caught.value) == "SMOKE_CLEANUP_COMMAND_FAILED"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-down-detail" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert len([call for call in runner.calls if "down" in call]) == 1
    assert list((tmp_path / ".secrets").iterdir()) == []


@pytest.mark.parametrize(
    ("first_failure", "reject_notes"),
    [("workflow", False), ("down", False), ("workflow", True)],
)
def test_smoke_preserves_first_exception_while_all_later_cleanup_still_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_failure: str,
    reject_notes: bool,
) -> None:
    primary_error: BaseException = (
        NoteRejectingError()
        if reject_notes
        else KeyboardInterrupt("private-primary-detail")
    )

    class DownFailingRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if "down" in argv:
                if first_failure == "down":
                    raise primary_error
                raise subprocess.CalledProcessError(
                    1,
                    argv,
                    stderr="private-down-detail",
                )
            return result

    runner = DownFailingRunner()
    input_cleanup_calls = 0
    real_remove = compose_smoke._remove_private_input_namespace

    def fail_after_input_cleanup(*args: Any, **kwargs: Any) -> None:
        nonlocal input_cleanup_calls
        input_cleanup_calls += 1
        real_remove(*args, **kwargs)
        raise RuntimeError("private-input-cleanup-detail")

    def workflow(session: ComposeSession) -> None:
        session.up_started = True
        if first_failure == "workflow":
            raise primary_error

    monkeypatch.setattr(
        compose_smoke,
        "_remove_private_input_namespace",
        fail_after_input_cleanup,
    )

    with pytest.raises(type(primary_error)) as caught:
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker-compose",),
            runner=runner,
            workflow=workflow,
            input_root=tmp_path / ".secrets",
        )

    assert caught.value is primary_error
    if reject_notes:
        assert isinstance(primary_error, NoteRejectingError)
        assert primary_error.attempted_notes == [
            "SMOKE_CLEANUP_COMMAND_FAILED",
            "SMOKE_INPUT_CLEANUP_FAILED",
        ]
        assert not hasattr(primary_error, "__notes__")
    else:
        assert caught.value.__notes__ == (
            ["SMOKE_CLEANUP_COMMAND_FAILED", "SMOKE_INPUT_CLEANUP_FAILED"]
            if first_failure == "workflow"
            else ["SMOKE_INPUT_CLEANUP_FAILED"]
        )
    assert len([call for call in runner.calls if "down" in call]) == 1
    assert input_cleanup_calls == 1
    assert list((tmp_path / ".secrets").iterdir()) == []
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-down-detail" not in rendered
    assert "private-input-cleanup-detail" not in rendered


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


def test_barrier_recovery_start_inherits_the_resolved_standalone_command() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"), Path("docker-compose.smoke.yml")),
        compose_command=("/usr/bin/docker-compose",),
        up_started=True,
    )

    barrier = compose_smoke._run_barrier_recovery_start(session)

    assert barrier.compose_command == ("/usr/bin/docker-compose",)
    assert barrier.files == (
        *session.files,
        compose_smoke._ROOT / "docker-compose.barrier.yml",
    )
    call = runner.calls[-1]
    assert call[0] == "/usr/bin/docker-compose"
    assert call.count("--profile") == 1
    assert call[call.index("--profile") + 1] == "m7-channels"
    assert str(compose_smoke._ROOT / "docker-compose.barrier.yml") in call
    assert call[-5:] == (
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "worker",
    )


def test_full_workflow_passes_the_resolved_session_to_the_barrier_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UntilBarrierRunner(RecordingRunner):
        def __call__(
            self, argv: Any, *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            call = tuple(argv)
            if call[:2] == ("/usr/bin/docker", "inspect"):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="0\n",
                    stderr="",
                )
            return result

    class BarrierReachedError(RuntimeError):
        pass

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=UntilBarrierRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"), Path("docker-compose.smoke.yml")),
        compose_command=("/usr/bin/docker-compose",),
    )
    received: list[ComposeSession] = []

    monkeypatch.setattr(compose_smoke, "_container_id", lambda *_, **__: "migrate")
    monkeypatch.setattr(compose_smoke, "_wait_ready", lambda **_: None)
    monkeypatch.setattr(
        compose_smoke,
        "_require_disabled_channel_entrypoints",
        lambda _: None,
    )
    monkeypatch.setattr(compose_smoke, "_start_web", lambda _: None)
    monkeypatch.setattr(compose_smoke, "_submit", lambda *_, **__: "task-id")
    monkeypatch.setattr(
        compose_smoke,
        "_wait_task",
        lambda *_, **__: {"status": "succeeded"},
    )
    monkeypatch.setattr(compose_smoke, "_normalised_evidence", lambda *_: ())

    def reach_barrier(candidate: ComposeSession) -> ComposeSession:
        received.append(candidate)
        assert candidate is session
        assert candidate.compose_command == ("/usr/bin/docker-compose",)
        raise BarrierReachedError

    monkeypatch.setattr(
        compose_smoke,
        "_run_barrier_recovery_start",
        reach_barrier,
    )

    with pytest.raises(BarrierReachedError):
        compose_smoke._full_workflow(session)

    assert received == [session]


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
    observed_paths: tuple[Path, ...] = ()
    observed_sensitive_values: tuple[str, ...] = ()

    def workflow(session: ComposeSession) -> None:
        nonlocal observed_paths, observed_sensitive_values
        observed_sensitive_values = session.sensitive_values
        override = json.loads(session.files[-1].read_text(encoding="utf-8"))
        postgres = Path(override["secrets"]["postgres_password"]["file"])
        feishu = Path(override["secrets"]["feishu_app_secret"]["file"])
        identities = Path(
            override["services"]["web-app"]["volumes"][0]["source"]
        )
        observed_paths = (postgres, feishu, identities)
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
        input_root=parent,
    )

    assert len(observed_sensitive_values) == 2
    assert not any(path.exists() for path in observed_paths)


def test_smoke_uses_a_private_input_namespace_and_generated_compose_override(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / ".secrets"
    fixed_user_file = input_root / "postgres_password"
    input_root.mkdir(mode=0o700)
    fixed_user_file.write_text("user-owned\n", encoding="utf-8")
    observed_private_directory: Path | None = None

    def workflow(session: ComposeSession) -> None:
        nonlocal observed_private_directory
        override = session.files[-1]
        observed_private_directory = override.parent
        assert override.name == "compose-smoke-inputs.json"
        assert override.parent.parent == input_root
        assert re.fullmatch(r"compose-smoke-[0-9a-f]{32}", override.parent.name)
        assert stat.S_IMODE(override.parent.stat().st_mode) == 0o700
        document = json.loads(override.read_text(encoding="utf-8"))
        postgres = Path(document["secrets"]["postgres_password"]["file"])
        feishu = Path(document["secrets"]["feishu_app_secret"]["file"])
        identity_mount = document["services"]["web-app"]["volumes"][0]
        identity = Path(identity_mount["source"])
        assert {path.parent for path in (postgres, feishu, identity)} == {
            override.parent
        }
        assert {path.name for path in (postgres, feishu, identity)} == {
            "postgres_password",
            "feishu_app_secret",
            "feishu-identities.json",
        }
        assert identity_mount["target"] == "/run/config/feishu-identities.json"
        assert identity_mount["read_only"] is True
        assert document["services"]["feishu-listener"]["volumes"] == [
            identity_mount
        ]
        override_text = override.read_text(encoding="utf-8")
        assert all(value not in override_text for value in session.sensitive_values)

    run_smoke(
        docker="/usr/bin/docker",
        compose_command=("/usr/bin/docker-compose",),
        runner=RecordingRunner(),
        workflow=workflow,
        input_root=input_root,
    )

    assert observed_private_directory is not None
    assert not observed_private_directory.exists()
    assert fixed_user_file.read_text(encoding="utf-8") == "user-owned\n"


def test_partial_private_input_failure_cleans_only_the_private_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / ".secrets"
    input_root.mkdir(mode=0o700)
    fixed_user_file = input_root / "feishu_app_secret"
    fixed_user_file.write_text("user-owned\n", encoding="utf-8")
    real_create = compose_smoke._create_input
    calls = 0

    def fail_third_input(path: Path, content: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SmokeError("SMOKE_INPUT_CREATE_FAILED")
        return real_create(path, content)

    monkeypatch.setattr(compose_smoke, "_create_input", fail_third_input)

    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_CREATE_FAILED$"):
        run_smoke(
            docker="/usr/bin/docker",
            compose_command=("/usr/bin/docker-compose",),
            runner=RecordingRunner(),
            workflow=lambda _: pytest.fail("workflow must not run"),
            input_root=input_root,
        )

    assert fixed_user_file.read_text(encoding="utf-8") == "user-owned\n"
    assert list(input_root.iterdir()) == [fixed_user_file]


@pytest.mark.parametrize(
    ("failure_at", "error_type"),
    [
        (2, KeyboardInterrupt),
        (2, RuntimeError),
        (3, KeyboardInterrupt),
        (3, RuntimeError),
    ],
)
def test_smoke_input_bundle_cleans_prior_files_after_any_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: int,
    error_type: type[BaseException],
) -> None:
    input_root = tmp_path / ".secrets"
    real_create = compose_smoke._create_input
    injected = error_type("private-input-interrupt-detail")
    calls = 0

    def fail_selected_input(path: Path, content: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise injected
        return real_create(path, content)

    monkeypatch.setattr(compose_smoke, "_create_input", fail_selected_input)

    with pytest.raises(error_type) as caught:
        compose_smoke._create_smoke_inputs(input_root=input_root)

    assert caught.value is injected
    assert input_root.is_dir()
    assert list(input_root.iterdir()) == []


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("cleanup_error_type", [CleanupBoundaryError, RuntimeError])
def test_smoke_input_bundle_preserves_the_first_error_when_cleanup_also_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    cleanup_error_type: type[BaseException],
) -> None:
    input_root = tmp_path / ".secrets"
    real_create = compose_smoke._create_input
    real_remove = compose_smoke._remove_private_input_namespace
    primary_error = error_type("private-primary-detail")
    cleanup_error = cleanup_error_type("private-cleanup-detail")
    create_calls = 0
    cleanup_calls = 0

    def fail_second_input(path: Path, content: str) -> Any:
        nonlocal create_calls
        create_calls += 1
        if create_calls == 2:
            raise primary_error
        return real_create(path, content)

    def fail_after_cleanup(*args: Any, **kwargs: Any) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        real_remove(*args, **kwargs)
        raise cleanup_error

    monkeypatch.setattr(compose_smoke, "_create_input", fail_second_input)
    monkeypatch.setattr(
        compose_smoke,
        "_remove_private_input_namespace",
        fail_after_cleanup,
    )

    with pytest.raises(error_type) as caught:
        compose_smoke._create_smoke_inputs(input_root=input_root)

    assert caught.value is primary_error
    assert caught.value.__notes__ == ["SMOKE_INPUT_CLEANUP_FAILED"]
    assert cleanup_calls == 1
    assert list(input_root.iterdir()) == []


def test_smoke_input_bundle_cleanup_preserves_a_note_rejecting_base_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_root = tmp_path / ".secrets"
    real_create = compose_smoke._create_input
    real_remove = compose_smoke._remove_private_input_namespace
    primary_error = NoteRejectingError()
    create_calls = 0
    cleanup_calls = 0

    def fail_second_input(path: Path, content: str) -> Any:
        nonlocal create_calls
        create_calls += 1
        if create_calls == 2:
            raise primary_error
        return real_create(path, content)

    def fail_after_cleanup(*args: Any, **kwargs: Any) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        real_remove(*args, **kwargs)
        raise RuntimeError("private-bundle-cleanup-detail")

    monkeypatch.setattr(compose_smoke, "_create_input", fail_second_input)
    monkeypatch.setattr(
        compose_smoke,
        "_remove_private_input_namespace",
        fail_after_cleanup,
    )

    with pytest.raises(NoteRejectingError) as caught:
        compose_smoke._create_smoke_inputs(input_root=input_root)

    assert caught.value is primary_error
    assert primary_error.attempted_notes == ["SMOKE_INPUT_CLEANUP_FAILED"]
    assert cleanup_calls == 1
    assert list(input_root.iterdir()) == []


@pytest.mark.parametrize("existing_name", ["feishu_app_secret", "feishu-identities.json"])
def test_private_input_namespace_preserves_fixed_user_files(
    tmp_path: Path, existing_name: str
) -> None:
    parent = tmp_path / ".secrets"
    parent.mkdir(mode=0o700)
    existing = parent / existing_name
    existing.write_text("user-owned\n", encoding="utf-8")
    workflow_called = False

    def workflow(_: ComposeSession) -> None:
        nonlocal workflow_called
        workflow_called = True

    run_smoke(
        docker="/usr/bin/docker",
        compose_command=("/usr/bin/docker-compose",),
        runner=RecordingRunner(),
        workflow=workflow,
        input_root=parent,
    )

    assert workflow_called is True
    assert existing.read_text(encoding="utf-8") == "user-owned\n"
    assert list(parent.iterdir()) == [existing]


def test_cleanup_refuses_a_replaced_parent_directory_and_preserves_new_files(
    tmp_path: Path,
) -> None:
    parent = tmp_path / ".secrets"
    moved = tmp_path / "owned-inputs"
    postgres = parent / "postgres_password"

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
            input_root=parent,
        )

    assert postgres.read_text(encoding="utf-8") == "user-owned\n"
    assert any(path.name.startswith("compose-smoke-") for path in moved.iterdir())


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


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
def test_input_creation_cleans_its_file_without_masking_a_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    path = tmp_path / ".secrets" / "feishu_app_secret"
    real_open = os.open
    real_fstat = os.fstat
    created_descriptor: int | None = None

    def track_open(target: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal created_descriptor
        descriptor = real_open(target, *args, **kwargs)
        if target == path.name and kwargs.get("dir_fd") is not None:
            created_descriptor = descriptor
        return descriptor

    def interrupt_fchmod(_: int, __: int) -> None:
        raise error_type("private-interrupt-detail")

    monkeypatch.setattr(compose_smoke.os, "open", track_open)
    monkeypatch.setattr(compose_smoke.os, "fchmod", interrupt_fchmod)

    with pytest.raises(error_type, match="private-interrupt-detail"):
        compose_smoke._create_input(path, "fake-value\n")

    assert not path.exists()
    assert created_descriptor is not None
    with pytest.raises(OSError):
        real_fstat(created_descriptor)


def test_unreturned_input_cleanup_preserves_a_note_rejecting_base_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".secrets" / "feishu_app_secret"
    primary_error = NoteRejectingError()
    cleanup_attempts = 0

    def fail_fchmod(_: int, __: int) -> None:
        raise primary_error

    def fail_unlink(*_: Any, **__: Any) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        raise RuntimeError("private-unreturned-cleanup-detail")

    monkeypatch.setattr(compose_smoke.os, "fchmod", fail_fchmod)
    monkeypatch.setattr(compose_smoke.os, "unlink", fail_unlink)

    with pytest.raises(NoteRejectingError) as caught:
        compose_smoke._create_input(path, "fake-value\n")

    assert caught.value is primary_error
    assert primary_error.attempted_notes == ["SMOKE_INPUT_CLEANUP_FAILED"]
    assert cleanup_attempts == 1
    assert path.exists()


def test_failed_input_creation_does_not_delete_a_replacement_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".secrets" / "feishu_app_secret"
    displaced = tmp_path / ".secrets" / "displaced-owned-input"

    def replace_then_fail(_: int, __: int) -> None:
        path.rename(displaced)
        path.write_text("user-owned\n", encoding="utf-8")
        raise OSError("private-fchmod-detail")

    monkeypatch.setattr(compose_smoke.os, "fchmod", replace_then_fail)

    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_CREATE_FAILED$"):
        compose_smoke._create_input(path, "fake-value\n")

    assert path.read_text(encoding="utf-8") == "user-owned\n"
    assert displaced.read_text(encoding="utf-8") == "fake-value\n"


def test_private_namespace_cleanup_preserves_a_replacement_after_atomic_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = compose_smoke._create_smoke_inputs(input_root=tmp_path / ".secrets")
    namespace = bundle.namespace
    real_rename = os.rename
    replacement_created = False

    def capture_then_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replacement_created
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if source == namespace.path.name and src_dir_fd == dst_dir_fd:
            namespace.path.mkdir(mode=0o700)
            (namespace.path / "user-file").write_text(
                "user-owned\n", encoding="utf-8"
            )
            replacement_created = True

    monkeypatch.setattr(compose_smoke.os, "rename", capture_then_replace)

    compose_smoke._remove_private_input_namespace(
        namespace,
        bundle.owned_inputs,
    )

    assert replacement_created is True
    assert (namespace.path / "user-file").read_text(encoding="utf-8") == (
        "user-owned\n"
    )


def test_private_namespace_cleanup_never_recursively_deletes_unknown_content(
    tmp_path: Path,
) -> None:
    bundle = compose_smoke._create_smoke_inputs(input_root=tmp_path / ".secrets")
    unknown = bundle.namespace.path / "unexpected"
    unknown.mkdir()
    (unknown / "user-file").write_text("user-owned\n", encoding="utf-8")
    with pytest.raises(SmokeError, match=r"^SMOKE_INPUT_CLEANUP_FAILED$"):
        compose_smoke._remove_private_input_namespace(
            bundle.namespace,
            bundle.owned_inputs,
        )

    retired = list(bundle.namespace.root.glob(".compose-smoke-retired-*"))
    assert len(retired) == 1
    preserved = retired[0]
    assert (preserved / "unexpected" / "user-file").read_text(
        encoding="utf-8"
    ) == "user-owned\n"


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


def test_main_passes_the_resolved_compose_command_to_run_smoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolved = ("/usr/bin/docker-compose",)
    captured: dict[str, object] = {}
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        compose_smoke,
        "_resolve_compose_command",
        lambda **_: resolved,
    )

    def capture_smoke(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(compose_smoke, "run_smoke", capture_smoke)

    assert compose_smoke.main() == 0
    assert capsys.readouterr().out == "compose-smoke: passed\n"
    assert captured["docker"] == "/usr/bin/docker"
    assert captured["compose_command"] is resolved
    assert captured["workflow"] is compose_smoke._full_workflow


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

    class Opener:
        def open(self, url: str, *, timeout: float) -> Response:
            calls.append((url, timeout))
            return Response()

    monkeypatch.setattr(
        compose_smoke, "_build_readiness_opener", lambda: Opener()
    )

    compose_smoke._wait_ready(web=True, timeout=0.1)

    assert calls == [("http://127.0.0.1:8080/readyz", 2.0)]


def test_readiness_opener_ignores_hostile_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setattr(
        compose_smoke.urllib.request,
        "getproxies",
        lambda: pytest.fail("host proxy discovery must not run"),
    )

    opener = compose_smoke._build_readiness_opener()

    assert not any(
        isinstance(handler, compose_smoke.urllib.request.ProxyHandler)
        and handler.proxies
        for handler in opener.handlers
    )


def test_readiness_redirect_is_rejected_without_an_external_second_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_url = "http://127.0.0.1:8080/readyz"
    external_url = "https://open.feishu.cn/provider-path"
    calls: list[str] = []

    class RedirectTransport(compose_smoke.urllib.request.BaseHandler):
        handler_order = 100

        def http_open(self, request: Any) -> Any:
            calls.append(request.full_url)
            headers = Message()
            if request.full_url == local_url:
                headers["Location"] = external_url
                response = addinfourl(BytesIO(), headers, local_url, 302)
                response.msg = "Found"
                return response
            response = addinfourl(BytesIO(), headers, external_url, 200)
            response.msg = "OK"
            return response

        def https_open(self, request: Any) -> Any:
            return self.http_open(request)

    real_build_opener = compose_smoke.urllib.request.build_opener

    def build_with_redirect_transport(*handlers: Any) -> Any:
        return real_build_opener(*handlers, RedirectTransport())

    monkeypatch.setattr(
        compose_smoke.urllib.request,
        "build_opener",
        build_with_redirect_transport,
    )
    moments = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(compose_smoke.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(compose_smoke.time, "sleep", lambda _: None)

    with pytest.raises(SmokeError, match=r"^SMOKE_READINESS_TIMEOUT$"):
        compose_smoke._wait_ready(web=True, timeout=0.1)

    assert calls == [local_url]


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


class WebBoundaryRunner(RecordingRunner):
    def __init__(
        self,
        *,
        payload: str | None = None,
        euid: str = "1000",
        fail_euid: bool = False,
    ) -> None:
        super().__init__()
        self.payload = payload or _web_inspect_payload()
        self.euid = euid
        self.fail_euid = fail_euid

    def __call__(
        self, argv: Any, *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        result = super().__call__(argv, timeout=timeout)
        call = tuple(argv)
        if call[-3:] == ("ps", "--quiet", "web-app"):
            return subprocess.CompletedProcess(
                argv, 0, stdout="web-container\n", stderr=""
            )
        if call[:2] == ("/usr/bin/docker", "inspect"):
            return subprocess.CompletedProcess(
                argv, 0, stdout=self.payload, stderr=""
            )
        if call[-6:-1] == ("exec", "-T", "web-app", "python", "-c"):
            if self.fail_euid:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="private-euid-command-output"
                )
            return subprocess.CompletedProcess(argv, 0, stdout=self.euid, stderr="")
        return result


def test_web_container_boundary_accepts_non_root_readonly_reference_mounts() -> None:
    runner = WebBoundaryRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
        sensitive_values=("private-fake-secret",),
    )
    compose_smoke._require_web_container_boundary(session)
    assert "os.geteuid" in runner.calls[-1][-1]


def test_web_container_boundary_rejects_an_actual_root_process() -> None:
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=WebBoundaryRunner(euid="0"),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_WEB_CONTAINER_BOUNDARY_INVALID$"):
        compose_smoke._require_web_container_boundary(session)


@pytest.mark.parametrize("euid", ["", "1000\n", "+1000", "1 000", "1\n2", "root"])
def test_web_container_boundary_rejects_a_malformed_euid(euid: str) -> None:
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=WebBoundaryRunner(euid=euid),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_WEB_EUID_PROTOCOL_ERROR$"):
        compose_smoke._require_web_container_boundary(session)


def test_web_euid_command_failure_exposes_only_a_fixed_error() -> None:
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=WebBoundaryRunner(fail_euid=True),
        project="isolated",
        files=(Path("docker-compose.yml"),),
        compose_command=("/usr/bin/docker", "compose"),
    )

    with pytest.raises(
        SmokeError, match=r"^SMOKE_WEB_EUID_COMMAND_FAILED$"
    ) as caught:
        compose_smoke._require_web_container_boundary(session)

    assert caught.value.__context__ is None
    assert "private-euid-command-output" not in str(caught.value)


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
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=WebBoundaryRunner(payload=payload),
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

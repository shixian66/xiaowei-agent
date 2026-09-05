"""smoke 资源归属、超时与清理命令的反例。"""

import re
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
            runner=runner,
            workflow=workflow,
            secret_path=tmp_path / "secret",
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
            runner=runner,
            workflow=workflow,
            secret_path=tmp_path / "secret",
        )
    down = [call for call in runner.calls if "down" in call]
    assert len(down) == 1
    assert "--volumes" in down[0]
    assert "--remove-orphans" in down[0]
    assert not (tmp_path / "secret").exists()


def test_project_names_have_a_full_random_uuid_suffix() -> None:
    first = project_name()
    second = project_name()
    assert first != second
    assert re.fullmatch(r"xiaowei_m5_smoke_[0-9a-f]{32}", first)


def test_missing_docker_is_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: None)
    assert compose_smoke.main() == 1
    assert capsys.readouterr().err == "compose-smoke: docker_not_found\n"


def test_main_reports_only_the_fixed_smoke_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        compose_smoke,
        "run_smoke",
        lambda **_: (_ for _ in ()).throw(SmokeError("SMOKE_BASELINE_COMMAND_FAILED")),
    )

    assert compose_smoke.main() == 1
    assert capsys.readouterr().err == "compose-smoke: SMOKE_BASELINE_COMMAND_FAILED\n"


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
    )
    session.failure_code = "SMOKE_BUILD_COMMAND_FAILED"

    with pytest.raises(SmokeError, match=r"^SMOKE_BUILD_COMMAND_FAILED$") as caught:
        session.run("build")
    assert caught.value.__context__ is None
    assert "private-driver-output" not in str(caught.value)


def test_exited_migration_is_looked_up_with_all_containers() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
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
    )
    with pytest.raises(SmokeError, match="SMOKE_WORKER_SCALE_INVALID"):
        compose_smoke._require_worker_scale(session)

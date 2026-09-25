"""W5 release smoke 的离线反例：候选镜像、部署模板、容器面与零执行足迹。

真实 Docker 运行由 CI 的 compose-smoke 门承担；这里用命令替身钉住每一步的
固定失败码、清理顺序与"只接受真实 digest 引用"。
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import compose_smoke, release_compose
from scripts.compose_smoke import ComposeSession, SmokeError

_DOCKER = "/usr/bin/docker"
_COMPOSE = ("/usr/bin/docker", "compose")
_REGISTRY_ID = "ab" * 32
_DIGEST = "sha256:" + "cd" * 32
_REFERENCE = f"localhost:32779/xiaowei-agent@{_DIGEST}"


class CandidateRunner:
    """按 argv 形状回放 build / registry / push / inspect / 清理。"""

    def __init__(
        self,
        *,
        fail: str | None = None,
        digests: list[str] | None = None,
        push_failures: int = 0,
    ) -> None:
        self.fail = fail
        self.digests = [_REFERENCE, "xiaowei-agent@" + _DIGEST] if digests is None else digests
        self.push_failures = push_failures
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout > 0
        call = tuple(argv)
        self.calls.append(call)
        stdout = ""
        if "build" in call:
            step = "build"
        elif call[1:3] == ("run", "-d"):
            step, stdout = "registry", _REGISTRY_ID + "\n"
        elif call[1] == "port":
            step, stdout = "port", "127.0.0.1:32779\n"
        elif call[1] == "tag":
            step = "tag"
        elif call[1] == "push":
            step = "push"
            if self.push_failures:
                self.push_failures -= 1
                raise subprocess.CalledProcessError(1, argv)
        elif call[1:3] == ("image", "inspect"):
            step, stdout = "inspect", json.dumps(self.digests)
        elif call[1:3] == ("image", "rm"):
            step = "untag"
        elif call[1:3] == ("rm", "-f"):
            step = "registry-rm"
        else:
            raise AssertionError(call)
        if step == self.fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(call, 0, stdout=stdout, stderr="")

    def steps(self) -> list[str]:
        names = []
        for call in self.calls:
            if "build" in call:
                names.append("build")
            elif call[1:3] == ("run", "-d"):
                names.append("registry")
            elif call[1:3] == ("image", "rm"):
                names.append("untag")
            elif call[1:3] == ("rm", "-f"):
                names.append("registry-rm")
            else:
                names.append(call[1])
        return names


def _candidate(runner: CandidateRunner) -> str:
    with compose_smoke._release_candidate(
        docker=_DOCKER, compose_command=_COMPOSE, runner=runner
    ) as image:
        return image


def test_candidate_is_built_from_the_base_dockerfile_and_pushed_by_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPOSE_XIAOWEI_IMAGE_TAG", raising=False)
    runner = CandidateRunner()

    assert _candidate(runner) == _REFERENCE
    build = runner.calls[0]
    assert build[: len(_COMPOSE)] == _COMPOSE
    assert build[-2:] == ("build", "migrate")
    assert build.count("-f") == 1
    assert build[build.index("-f") + 1].endswith("/docker-compose.yml")
    registry = runner.calls[1]
    assert registry[-1] == compose_smoke._REGISTRY_IMAGE
    assert "@sha256:" in compose_smoke._REGISTRY_IMAGE
    assert "127.0.0.1::5000" in registry
    assert runner.calls[3] == (
        _DOCKER,
        "tag",
        "xiaowei-agent:m5-local",
        "localhost:32779/xiaowei-agent:release-candidate",
    )
    assert runner.steps()[-2:] == ["untag", "registry-rm"]
    assert runner.calls[-1] == (_DOCKER, "rm", "-f", _REGISTRY_ID)


def test_candidate_waits_for_the_registry_to_accept_the_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compose_smoke.time, "sleep", lambda _: None)
    runner = CandidateRunner(push_failures=3)
    assert _candidate(runner) == _REFERENCE
    assert runner.steps().count("push") == 4


@pytest.mark.parametrize(
    "digests",
    [
        [],
        ["xiaowei-agent@" + _DIGEST],
        ["localhost:40000/xiaowei-agent@" + _DIGEST],
        ["localhost:32779/xiaowei-agent@sha256:short"],
        [_REFERENCE, "localhost:32779/xiaowei-agent@sha256:" + "ef" * 32],
    ],
)
def test_candidate_accepts_only_one_digest_from_the_ephemeral_registry(
    digests: list[str],
) -> None:
    runner = CandidateRunner(digests=digests)
    with pytest.raises(SmokeError, match=r"^SMOKE_RELEASE_DIGEST_INVALID$"):
        _candidate(runner)
    assert runner.steps()[-2:] == ["untag", "registry-rm"]


def test_build_failure_starts_no_registry() -> None:
    runner = CandidateRunner(fail="build")
    with pytest.raises(SmokeError, match=r"^SMOKE_RELEASE_BUILD_FAILED$"):
        _candidate(runner)
    assert runner.steps() == ["build"]


@pytest.mark.parametrize("step", ["port", "tag", "inspect"])
def test_registry_is_removed_after_any_later_failure(step: str) -> None:
    runner = CandidateRunner(fail=step)
    with pytest.raises(SmokeError):
        _candidate(runner)
    assert runner.steps()[-1] == "registry-rm"


def test_cleanup_failure_is_a_note_not_a_replacement_of_the_workflow_error() -> None:
    runner = CandidateRunner(fail="registry-rm")
    with pytest.raises(RuntimeError, match="workflow") as caught:
        with compose_smoke._release_candidate(
            docker=_DOCKER, compose_command=_COMPOSE, runner=runner
        ):
            raise RuntimeError("workflow")
    assert "SMOKE_RELEASE_CLEANUP_FAILED" in getattr(caught.value, "__notes__", [])


def test_cleanup_failure_alone_is_a_fixed_code() -> None:
    runner = CandidateRunner(fail="untag")
    with pytest.raises(SmokeError, match=r"^SMOKE_RELEASE_CLEANUP_FAILED$"):
        _candidate(runner)


# --------------------------------------------------------------------------
# 部署模板与 session
# --------------------------------------------------------------------------


def test_release_smoke_template_is_the_closed_deployment_template() -> None:
    document = compose_smoke._release_env_document(image=_REFERENCE)
    values = dict(line.split("=", 1) for line in document.splitlines())
    assert set(values) == release_compose.RELEASE_TEMPLATE_KEYS
    assert values["XIAOWEI_RELEASE_IMAGE"] == _REFERENCE
    assert values["XIAOWEI_WEB_PUBLIC_ORIGIN"].endswith(".invalid")
    assert values["XIAOWEI_RELEASE_WEB_BIND_IP"] == "127.0.0.1"


def test_release_run_uses_the_release_override_and_its_template(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def workflow(session: ComposeSession) -> None:
        seen["files"] = [path.name for path in session.files]
        seen["argv"] = session.argv("config")
        assert session.env_file is not None
        seen["template"] = session.env_file.read_text(encoding="utf-8")

    class Runner:
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(tuple(argv), 0, stdout="", stderr="")

    compose_smoke.run_smoke(
        docker=_DOCKER,
        compose_command=_COMPOSE,
        runner=Runner(),
        workflow=workflow,
        input_root=tmp_path / ".secrets",
        release_image=_REFERENCE,
    )

    assert seen["files"] == [
        "docker-compose.yml",
        "docker-compose.release.yml",
        "compose-smoke-inputs.json",
    ]
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[2] == "--env-file"
    assert f"XIAOWEI_RELEASE_IMAGE={_REFERENCE}\n" in str(seen["template"])
    # 模板与其他输入一起在 run_smoke 结束时清掉。
    assert not list((tmp_path / ".secrets").glob("compose-smoke-*/release.env"))


def test_offline_run_keeps_the_smoke_override_and_no_template(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def workflow(session: ComposeSession) -> None:
        seen["files"] = [path.name for path in session.files]
        seen["env_file"] = session.env_file
        seen["argv"] = session.argv("config")

    class Runner:
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(tuple(argv), 0, stdout="", stderr="")

    compose_smoke.run_smoke(
        docker=_DOCKER,
        compose_command=_COMPOSE,
        runner=Runner(),
        workflow=workflow,
        input_root=tmp_path / ".secrets",
    )

    assert seen["files"] == [
        "docker-compose.yml",
        "docker-compose.smoke.yml",
        "compose-smoke-inputs.json",
    ]
    assert seen["env_file"] is None
    assert "--env-file" not in seen["argv"]  # type: ignore[operator]


# --------------------------------------------------------------------------
# 容器面与 Web 面
# --------------------------------------------------------------------------


def _inspect_session(payloads: dict[str, list[object]]) -> ComposeSession:
    class Runner:
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            call = tuple(argv)
            if "ps" in call:
                service = call[-1]
                return subprocess.CompletedProcess(call, 0, stdout=f"id-{service}\n", stderr="")
            if call[1] == "inspect":
                service = call[-1].removeprefix("id-")
                return subprocess.CompletedProcess(
                    call, 0, stdout=json.dumps(payloads[service]), stderr=""
                )
            raise AssertionError(call)

    return ComposeSession(
        docker=_DOCKER,
        runner=Runner(),
        project="isolated",
        files=(),
        compose_command=_COMPOSE,
    )


def _healthy_payloads() -> dict[str, list[object]]:
    environment = [
        "XIAOWEI_RUNTIME_PROFILE=release",
        "XIAOWEI_STARROCKS_ADAPTER_MODE=disabled",
    ]
    return {
        "api": [_REFERENCE, environment, {}],
        "worker": [_REFERENCE, environment, None],
        "web-app": [
            _REFERENCE,
            environment,
            {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]},
        ],
    }


def test_healthy_release_containers_pass() -> None:
    compose_smoke._require_release_containers(
        _inspect_session(_healthy_payloads()), image=_REFERENCE
    )


@pytest.mark.parametrize(
    ("service", "index", "value"),
    [
        ("worker", 0, "xiaowei-agent:m5-local"),
        ("api", 1, ["XIAOWEI_STARROCKS_ADAPTER_MODE=disabled"]),
        (
            "worker",
            1,
            [
                "XIAOWEI_RUNTIME_PROFILE=offline_recording",
                "XIAOWEI_STARROCKS_ADAPTER_MODE=disabled",
            ],
        ),
        (
            "api",
            1,
            [
                "XIAOWEI_RUNTIME_PROFILE=release",
                "XIAOWEI_STARROCKS_ADAPTER_MODE=disabled",
                "XIAOWEI_RELEASE_IMAGE=x",
            ],
        ),
        ("api", 2, {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}),
        ("web-app", 2, {"8080/tcp": [{"HostIp": "0.0." + "0.0", "HostPort": "8080"}]}),
    ],
)
def test_release_container_drift_is_rejected(service: str, index: int, value: object) -> None:
    payloads = _healthy_payloads()
    payloads[service][index] = value
    with pytest.raises(SmokeError, match=r"^SMOKE_RELEASE_CONTAINER_INVALID$"):
        compose_smoke._require_release_containers(
            _inspect_session(payloads), image=_REFERENCE
        )


def _web(status: int, headers: tuple[tuple[str, str], ...] = (), body: bytes = b""):
    return compose_smoke._WebProbeResponse(status=status, headers=headers, body=body)


def _surface(monkeypatch: pytest.MonkeyPatch, responses: dict[tuple[str, str], Any]) -> None:
    monkeypatch.setattr(
        compose_smoke,
        "_request_web",
        lambda path, *, host: responses[(path, host)],
    )


def _healthy_surface() -> dict[tuple[str, str], Any]:
    return {
        ("/login", compose_smoke._WEB_PUBLIC_HOST): _web(200),
        ("/login", compose_smoke._WEB_DIRECT_HOST): _web(
            403, body=compose_smoke._WEB_FORBIDDEN_BODY
        ),
        ("/oauth/feishu/start", compose_smoke._WEB_PUBLIC_HOST): _web(404),
    }


def test_release_web_surface_accepts_login_and_no_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    _surface(monkeypatch, _healthy_surface())
    compose_smoke._require_release_web_surface()


@pytest.mark.parametrize(
    ("key", "response"),
    [
        (("/login", "sso.example.invalid"), _web(500)),
        (("/login", "127.0.0.1:8080"), _web(200)),
        (
            ("/oauth/feishu/start", "sso.example.invalid"),
            _web(302, (("Location", "https://open.feishu.cn/"),)),
        ),
        (
            ("/oauth/feishu/start", "sso.example.invalid"),
            _web(404, (("Set-Cookie", "state=x"),)),
        ),
    ],
)
def test_release_web_surface_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch, key: tuple[str, str], response: Any
) -> None:
    responses = _healthy_surface()
    responses[key] = response
    _surface(monkeypatch, responses)
    with pytest.raises(SmokeError, match=r"^SMOKE_RELEASE_WEB_SURFACE_INVALID$"):
        compose_smoke._require_release_web_surface()


# --------------------------------------------------------------------------
# 工作流：最终模型、一次性命令、零执行足迹
# --------------------------------------------------------------------------


class _Stubs:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.status = "rejected"
        self.footprint = "0|0|0"
        self.retention: object = dict(compose_smoke._RELEASE_RETENTION_REPORT)
        self.identity: object = dict(compose_smoke._RELEASE_IDENTITY_REPORT)
        self.violations: tuple[str, ...] = ()
        record = self.events.append
        monkeypatch.setattr(compose_smoke, "_require_migrated", lambda *_: record("migrate"))
        monkeypatch.setattr(compose_smoke, "_wait_ready", lambda **_: record("ready"))
        monkeypatch.setattr(
            compose_smoke, "_require_release_containers", lambda *_, **__: record("containers")
        )
        monkeypatch.setattr(
            compose_smoke, "_require_web_container_boundary", lambda *_: record("boundary")
        )
        monkeypatch.setattr(
            compose_smoke, "_require_release_web_surface", lambda: record("surface")
        )
        monkeypatch.setattr(compose_smoke, "_submit", lambda *_, **__: "task")
        monkeypatch.setattr(
            compose_smoke, "_wait_task", lambda *_, **__: {"status": self.status}
        )
        monkeypatch.setattr(compose_smoke, "_psql", lambda *_: self.footprint)
        monkeypatch.setattr(compose_smoke, "_require_logs_clean", lambda *_: record("logs"))
        monkeypatch.setattr(
            release_compose, "release_violations", lambda _: self.violations
        )

        def one_shot(_: ComposeSession, module: str, *, failure_code: str) -> object:
            record(module.rsplit(".", 1)[1])
            return self.retention if "retention" in module else self.identity

        monkeypatch.setattr(compose_smoke, "_one_shot", one_shot)


def _workflow_session() -> ComposeSession:
    class Runner:
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            call = tuple(argv)
            stdout = json.dumps({"services": {}}) if "config" in call else ""
            return subprocess.CompletedProcess(call, 0, stdout=stdout, stderr="")

    return ComposeSession(
        docker=_DOCKER,
        runner=Runner(),
        project="isolated",
        files=(),
        compose_command=_COMPOSE,
    )


def test_release_workflow_runs_every_gate_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    stubs = _Stubs(monkeypatch)
    compose_smoke._release_workflow(_workflow_session(), image=_REFERENCE)
    assert stubs.events == [
        "migrate",
        "activation_retention",
        "legacy_identity_migration",
        "ready",
        "containers",
        "boundary",
        "surface",
        "logs",
    ]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"violations": ("live_switch_enabled",)}, "SMOKE_RELEASE_MODEL_INVALID"),
        ({"retention": {"approved_deleted": 1}}, "SMOKE_RELEASE_RETENTION_FAILED"),
        ({"identity": {"status": "migrated"}}, "SMOKE_RELEASE_IDENTITY_FAILED"),
        ({"status": "succeeded"}, "SMOKE_RELEASE_TASK_NOT_REJECTED"),
        ({"status": "failed"}, "SMOKE_RELEASE_TASK_NOT_REJECTED"),
        ({"footprint": "1|0|0"}, "SMOKE_RELEASE_EXECUTION_PRESENT"),
        ({"footprint": "0|1|0"}, "SMOKE_RELEASE_EXECUTION_PRESENT"),
        ({"footprint": "0|0|1"}, "SMOKE_RELEASE_EXECUTION_PRESENT"),
    ],
)
def test_release_workflow_fails_closed_on_each_gate(
    monkeypatch: pytest.MonkeyPatch, mutation: dict[str, object], code: str
) -> None:
    stubs = _Stubs(monkeypatch)
    for name, value in mutation.items():
        setattr(stubs, name, value)
    with pytest.raises(SmokeError, match=rf"^{code}$"):
        compose_smoke._release_workflow(_workflow_session(), image=_REFERENCE)


def test_release_smoke_threads_the_candidate_into_run_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_smoke(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(compose_smoke, "run_smoke", fake_run_smoke)
    compose_smoke.run_release_smoke(
        docker=_DOCKER, compose_command=_COMPOSE, runner=CandidateRunner()
    )
    assert captured["release_image"] == _REFERENCE
    workflow = captured["workflow"]
    assert getattr(workflow, "func", None) is compose_smoke._release_workflow
    assert getattr(workflow, "keywords", None) == {"image": _REFERENCE}

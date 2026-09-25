"""W5 provider-off release Compose：最终模型、部署模板与部署前检查命令的契约。

只读单个 YAML 看不出最终部署面：``!reset`` / ``!override`` 与 ``ports`` 的合并语义只有
渲染后才确定。所以本文件的承重断言都落在 ``docker compose config`` 的**合成结果**上，
静态断言只用来钉"开关是字面量、不是插值"这类渲染后看不出来的写法。
"""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import release_compose
from scripts.compose_smoke import _default_runner, _resolve_compose_command

_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_FILES = ("docker-compose.yml", "docker-compose.release.yml")
_APP_SERVICES = {
    "migrate",
    "api",
    "worker",
    "web-app",
    "feishu-listener",
    "channel-worker",
}
_DEFAULT_SERVICES = {"postgres", "migrate", "api", "worker", "web-app"}
_IMAGE = "registry.example.invalid/xiaowei-agent@sha256:" + "ab" * 32
_TEMPLATE = {
    "XIAOWEI_RELEASE_IMAGE": _IMAGE,
    "XIAOWEI_RELEASE_ACTOR": "release-admin",
    "XIAOWEI_RELEASE_SOURCE_SHA": "0" * 40,
    "XIAOWEI_RELEASE_EDGE_EVIDENCE_REF": "edge-evidence-2026-09",
    "XIAOWEI_RELEASE_WEB_BIND_IP": "127.0.0.1",
    "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://sso.example.invalid",
}


class _ComposeLoader(yaml.SafeLoader):
    """``!override`` / ``!reset`` 是渲染期指令，静态读取时按普通值对待。"""


def _construct_tagged(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _construct_tagged)


def _release_yaml() -> dict[str, Any]:
    value = yaml.load(
        (_ROOT / "docker-compose.release.yml").read_text(encoding="utf-8"),
        Loader=_ComposeLoader,  # noqa: S506 -- 派生自 SafeLoader，只多认两个标签
    )
    assert isinstance(value, dict)
    return value


def _compose_command() -> tuple[str, ...]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable; compose-smoke remains the no-skip gate")
    return _resolve_compose_command(docker=docker, runner=_default_runner)


def _clean_environment(**template: str) -> dict[str, str]:
    """宿主环境去掉全部 ``XIAOWEI_*`` / ``COMPOSE_*`` 后再叠加模板值。"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XIAOWEI_", "COMPOSE_"))
    }
    environment.update(template)
    return environment


def _render(
    *extra: Path,
    template: dict[str, str] | None = None,
    profiles: bool = True,
) -> dict[str, Any]:
    command = [*_compose_command()]
    if profiles:
        command.extend(("--profile", "m7-channels"))
    for name in _RELEASE_FILES:
        command.extend(("-f", str(_ROOT / name)))
    for path in extra:
        command.extend(("-f", str(path)))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(  # noqa: S603 -- binary 由 shutil.which 解析
        command,
        cwd=_ROOT,
        env=_clean_environment(**(_TEMPLATE if template is None else template)),
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


# --------------------------------------------------------------------------
# 静态写法：开关是字面量
# --------------------------------------------------------------------------


def test_release_override_is_registered_and_covers_every_app_service() -> None:
    release = _release_yaml()
    assert set(release["services"]) == _APP_SERVICES
    for name in _APP_SERVICES:
        service = release["services"][name]
        assert service["image"] == "${XIAOWEI_RELEASE_IMAGE:?required}", name
        assert "build" in service and service["build"] is None, name
    assert "secrets" not in release
    assert "volumes" not in release


def test_build_is_reset_directly_on_every_service_not_through_the_anchor() -> None:
    """Compose 2.38（CI runner）不对经 ``<<`` 锚点合并进来的 ``!reset`` 生效。

    合并后的静态值看不出差别，所以按原文逐行钉住：锚点里不得出现 ``build``，
    每个 app service 自己写 ``build: !reset null``。
    """
    text = (_ROOT / "docker-compose.release.yml").read_text(encoding="utf-8")
    assert "build" not in _release_yaml()["x-release-service"]
    assert text.count("    build: !reset null\n") == len(_APP_SERVICES)
    assert "  build: !reset null\n  labels" not in text


def test_release_switches_are_literals_the_template_cannot_override() -> None:
    """Compose 的 ``environment:`` 优先于 ``--env-file``；写成插值就等于把开关交给模板。"""
    services = _release_yaml()["services"]
    fixed = {
        "XIAOWEI_RUNTIME_PROFILE": "release",
        "XIAOWEI_ENVIRONMENT_ID": "dev",
        "XIAOWEI_STARROCKS_ADAPTER_MODE": "disabled",
        "XIAOWEI_SMOKE_STEP_BARRIER": "false",
        "XIAOWEI_GEMINI_ENABLED": "false",
    }
    for name in _APP_SERVICES:
        environment = services[name]["environment"]
        for key, value in fixed.items():
            assert environment[key] == value, (name, key)
    web = services["web-app"]["environment"]
    for key in (
        "XIAOWEI_FEISHU_OAUTH_ENABLED",
        "XIAOWEI_GEMINI_REAL_TEST_ENABLED",
        "XIAOWEI_FEISHU_REAL_TEST_ENABLED",
    ):
        assert web[key] == "false", key
    assert web["XIAOWEI_WEB_APP_ENABLED"] == "true"
    assert web["XIAOWEI_WEB_MODE"] == "https"
    assert services["feishu-listener"]["environment"][
        "XIAOWEI_FEISHU_LISTENER_ENABLED"
    ] == "false"
    assert services["channel-worker"]["environment"][
        "XIAOWEI_CHANNEL_WORKER_ENABLED"
    ] == "false"
    for name in _APP_SERVICES:
        for key, value in services[name]["environment"].items():
            if key in {"XIAOWEI_ACTOR", "XIAOWEI_WEB_PUBLIC_ORIGIN"}:
                assert value.endswith(":?required}"), (name, key)
            else:
                assert "${" not in str(value), (name, key)


# --------------------------------------------------------------------------
# 合成后的最终模型
# --------------------------------------------------------------------------


def test_rendered_release_is_the_only_legal_provider_off_model() -> None:
    model = _render()
    assert release_compose.release_violations(model) == ()
    assert set(model["services"]) == _DEFAULT_SERVICES | {
        "feishu-listener",
        "channel-worker",
    }
    for name in _APP_SERVICES:
        service = model["services"][name]
        assert service["image"] == _IMAGE
        assert "build" not in service
        environment = service["environment"]
        assert environment["XIAOWEI_RUNTIME_PROFILE"] == "release"
        assert environment["XIAOWEI_ACTOR"] == "release-admin"
        assert not any(key.startswith("XIAOWEI_RELEASE_") for key in environment)


def test_default_release_start_does_not_include_the_channel_profile() -> None:
    assert set(_render(profiles=False)["services"]) == _DEFAULT_SERVICES


def test_rendered_release_publishes_only_web_on_the_template_address() -> None:
    services = _render()["services"]
    assert services["web-app"]["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8080,
            "published": "8080",
            "protocol": "tcp",
        }
    ]
    for name in set(services) - {"web-app"}:
        assert not services[name].get("ports"), name


def test_release_keeps_the_config_mount_matrix_of_the_base_file() -> None:
    services = _render()["services"]
    expected = {
        "web-app": {"ai": False, "feishu": False, "resources": False},
        "worker": {"ai": True, "resources": True},
        "feishu-listener": {"feishu": True},
        "channel-worker": {"feishu": True},
        "api": {},
        "migrate": {},
    }
    for name, domains in expected.items():
        mounts = {
            volume["target"].rsplit("/", 1)[1]: bool(volume.get("read_only"))
            for volume in services[name].get("volumes", [])
        }
        assert mounts == domains, name


@pytest.mark.parametrize("missing", sorted(_TEMPLATE))
def test_every_template_variable_is_required(missing: str) -> None:
    template = {key: value for key, value in _TEMPLATE.items() if key != missing}
    command = [*_compose_command()]
    for name in _RELEASE_FILES:
        command.extend(("-f", str(_ROOT / name)))
    command.extend(("config", "--quiet"))
    result = subprocess.run(  # noqa: S603 -- binary 由 shutil.which 解析
        command,
        cwd=_ROOT,
        env=_clean_environment(**template),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    for value in _TEMPLATE.values():
        assert value not in result.stdout + result.stderr


# --------------------------------------------------------------------------
# 部署模板
# --------------------------------------------------------------------------


def _template_lines() -> dict[str, str]:
    lines: dict[str, str] = {}
    text = (_ROOT / "docs/examples/w5-release.env.example").read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, raw
        lines[key] = value
    return lines


def test_release_template_names_exactly_the_required_variables() -> None:
    lines = _template_lines()
    assert set(lines) == release_compose.RELEASE_TEMPLATE_KEYS == set(_TEMPLATE)
    # 模板只给变量名与格式说明，不给任何可直接部署的值——空值恰好会让渲染失败。
    assert all(value == "" for value in lines.values())


def test_release_template_is_rejected_until_filled(tmp_path: Path) -> None:
    template = tmp_path / "release.env"
    template.write_text(
        (_ROOT / "docs/examples/w5-release.env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    code, out, _ = _check(("--env-file", str(template)))
    assert code == 1
    assert out == "release-compose: release_env_template_invalid\n"


# --------------------------------------------------------------------------
# 部署前检查命令
# --------------------------------------------------------------------------


def _write_template(path: Path, values: dict[str, str]) -> Path:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return path


def _check(
    argv: tuple[str, ...], *, environ: dict[str, str] | None = None
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = release_compose.main(
        list(argv),
        environ=_clean_environment() if environ is None else environ,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_check_accepts_the_filled_template_and_prints_only_a_closed_line(
    tmp_path: Path,
) -> None:
    _compose_command()
    template = _write_template(tmp_path / "release.env", _TEMPLATE)
    code, out, err = _check(("--env-file", str(template)))
    assert (code, out, err) == (0, "release-compose: ok\n", "")


def test_check_never_echoes_template_values(tmp_path: Path) -> None:
    _compose_command()
    values = dict(_TEMPLATE, XIAOWEI_RELEASE_WEB_BIND_IP="0.0." + "0.0")
    template = _write_template(tmp_path / "release.env", values)
    code, out, err = _check(("--env-file", str(template)))
    assert code == 1
    assert out == "release-compose: port_surface_invalid\n"
    for value in values.values():
        if len(value) > 8:
            assert value not in out + err


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("--env-file",),
        ("--env-file", "a", "--env-file", "b"),
        ("--env-file", "a", "--unknown"),
    ],
)
def test_check_usage_errors_are_closed(argv: tuple[str, ...]) -> None:
    code, out, err = _check(argv)
    assert (code, out, err) == (2, "", "release-compose: usage_invalid\n")


@pytest.mark.parametrize(
    "files",
    [
        ("docker-compose.yml",),
        ("docker-compose.release.yml", "docker-compose.yml"),
        ("docker-compose.yml", "docker-compose.release.yml", "docker-compose.lan.yml"),
        ("docker-compose.yml", "docker-compose.release.yml", "docker-compose.model.yml"),
        ("docker-compose.yml", "docker-compose.smoke.yml"),
    ],
)
def test_check_rejects_any_other_file_set(tmp_path: Path, files: tuple[str, ...]) -> None:
    template = _write_template(tmp_path / "release.env", _TEMPLATE)
    argv: list[str] = ["--env-file", str(template)]
    for name in files:
        argv.extend(("-f", name))
    code, out, _ = _check(tuple(argv))
    assert (code, out) == (1, "release-compose: release_file_set_invalid\n")


def test_check_accepts_the_explicit_canonical_file_set(tmp_path: Path) -> None:
    _compose_command()
    template = _write_template(tmp_path / "release.env", _TEMPLATE)
    code, out, _ = _check(
        (
            "--env-file",
            str(template),
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.release.yml",
        )
    )
    assert (code, out) == (0, "release-compose: ok\n")


@pytest.mark.parametrize(
    "mutation",
    [
        {"XIAOWEI_GEMINI_ENABLED": "true"},
        {"COMPOSE_PROFILES": "m7-channels"},
        {"COMPOSE_FILE": "docker-compose.yml"},
    ],
)
def test_template_outside_the_closed_key_set_is_rejected(
    tmp_path: Path, mutation: dict[str, str]
) -> None:
    template = _write_template(tmp_path / "release.env", {**_TEMPLATE, **mutation})
    code, out, _ = _check(("--env-file", str(template)))
    assert (code, out) == (1, "release-compose: release_env_template_invalid\n")


@pytest.mark.parametrize(
    "variable",
    [
        "XIAOWEI_GEMINI_ENABLED",
        "XIAOWEI_RELEASE_IMAGE",
        "COMPOSE_PROFILES",
        "COMPOSE_FILE",
    ],
)
def test_shell_environment_that_compose_would_prefer_is_rejected(
    tmp_path: Path, variable: str
) -> None:
    """Compose 让 shell 环境优先于 ``--env-file``；检查通过的必须就是会部署的值。"""
    template = _write_template(tmp_path / "release.env", _TEMPLATE)
    environ = _clean_environment()
    environ[variable] = "x"
    code, out, _ = _check(("--env-file", str(template)), environ=environ)
    assert (code, out) == (1, "release-compose: shell_environment_invalid\n")


def test_missing_template_file_is_a_closed_failure(tmp_path: Path) -> None:
    code, out, _ = _check(("--env-file", str(tmp_path / "missing.env")))
    assert (code, out) == (1, "release-compose: release_env_template_invalid\n")

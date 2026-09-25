"""W5 provider-off release 的部署前检查：合成最终 Compose 模型，逐项核对发布面。

``python -m scripts.release_compose --env-file <部署模板>
[-f docker-compose.yml -f docker-compose.release.yml]``

- 唯一合法的文件集合是 ``docker-compose.yml`` + ``docker-compose.release.yml``；显式传入
  其他组合（多一份 LAN/model/smoke override，或顺序颠倒）直接拒绝。
- 部署模板的键是闭集：缺键、多键（包括 ``COMPOSE_PROFILES`` / ``COMPOSE_FILE``）都拒绝。
- Compose 让 shell 环境优先于 ``--env-file``；shell 里残留 ``XIAOWEI_*`` / ``COMPOSE_*``
  时拒绝，否则检查通过的值与实际部署的值可能不是同一份。
- 合成结果只在内存里核对，**绝不打印**解析后的配置、模板值或 Compose 的错误正文；
  输出只有一行 ``release-compose: ok`` 或逗号分隔的闭集失败码。

本模块属于部署工具面，不进 wheel/镜像。进程层另有 ``Settings`` 在 release 下拒绝任何
被打开的真实调用开关；两层互不代替。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, TextIO

_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_COMMAND: Final[str] = "release-compose"

RELEASE_COMPOSE_FILES: Final[tuple[str, ...]] = (
    "docker-compose.yml",
    "docker-compose.release.yml",
)
RELEASE_TEMPLATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "XIAOWEI_RELEASE_IMAGE",
        "XIAOWEI_RELEASE_ACTOR",
        "XIAOWEI_RELEASE_SOURCE_SHA",
        "XIAOWEI_RELEASE_EDGE_EVIDENCE_REF",
        "XIAOWEI_RELEASE_WEB_BIND_IP",
        "XIAOWEI_WEB_PUBLIC_ORIGIN",
    }
)
_CHANNEL_PROFILE: Final[str] = "m7-channels"

DEFAULT_SERVICES: Final[frozenset[str]] = frozenset(
    {"postgres", "migrate", "api", "worker", "web-app"}
)
CHANNEL_SERVICES: Final[frozenset[str]] = frozenset({"feishu-listener", "channel-worker"})
APP_SERVICES: Final[frozenset[str]] = (DEFAULT_SERVICES - {"postgres"}) | CHANNEL_SERVICES

_COMMANDS: Final[Mapping[str, str]] = {
    "migrate": "xiaowei_agent.interfaces.migrate",
    "api": "xiaowei_agent.interfaces.api",
    "worker": "xiaowei_agent.interfaces.worker",
    "web-app": "xiaowei_agent.interfaces.web_app",
    "feishu-listener": "xiaowei_agent.interfaces.feishu_listener",
    "channel-worker": "xiaowei_agent.interfaces.feishu_worker",
}
_CONFIG_TARGET: Final[str] = "/run/xiaowei-config"
# 服务 → {域: 是否只读}；与基础文件的 W4a 矩阵逐格相同，release 不改变谁能写配置。
_CONFIG_MATRIX: Final[Mapping[str, Mapping[str, bool]]] = {
    "migrate": {},
    "api": {},
    "worker": {"ai": True, "resources": True},
    "web-app": {"ai": False, "feishu": False, "resources": False},
    "feishu-listener": {"feishu": True},
    "channel-worker": {"feishu": True},
}
_POSTGRES_DATA_TARGET: Final[str] = "/var/lib/postgresql/data"
_POSTGRES_SECRET: Final[str] = "postgres_" + "password"

_FIXED_ENVIRONMENT: Final[Mapping[str, str]] = {
    "XIAOWEI_RUNTIME_PROFILE": "release",
    "XIAOWEI_ENVIRONMENT_ID": "dev",
    "XIAOWEI_STARROCKS_ADAPTER_MODE": "disabled",
    "XIAOWEI_SMOKE_STEP_BARRIER": "false",
}
_LIVE_SWITCHES: Final[frozenset[str]] = frozenset(
    {
        "XIAOWEI_GEMINI_ENABLED",
        "XIAOWEI_FEISHU_OAUTH_ENABLED",
        "XIAOWEI_FEISHU_LISTENER_ENABLED",
        "XIAOWEI_CHANNEL_WORKER_ENABLED",
        "XIAOWEI_GEMINI_REAL_TEST_ENABLED",
        "XIAOWEI_FEISHU_REAL_TEST_ENABLED",
    }
)
# 每个服务至少要显式收到哪些关闭开关；其余服务若出现这些键也必须是 "false"。
_REQUIRED_SWITCHES: Final[Mapping[str, frozenset[str]]] = {
    "migrate": frozenset({"XIAOWEI_GEMINI_ENABLED"}),
    "api": frozenset({"XIAOWEI_GEMINI_ENABLED"}),
    "worker": frozenset({"XIAOWEI_GEMINI_ENABLED"}),
    "web-app": frozenset(
        {
            "XIAOWEI_GEMINI_ENABLED",
            "XIAOWEI_FEISHU_OAUTH_ENABLED",
            "XIAOWEI_GEMINI_REAL_TEST_ENABLED",
            "XIAOWEI_FEISHU_REAL_TEST_ENABLED",
        }
    ),
    "feishu-listener": frozenset({"XIAOWEI_GEMINI_ENABLED", "XIAOWEI_FEISHU_LISTENER_ENABLED"}),
    "channel-worker": frozenset({"XIAOWEI_GEMINI_ENABLED", "XIAOWEI_CHANNEL_WORKER_ENABLED"}),
}
# 真实 Provider/目标的连接参数：release 里任何服务都不得携带。
_LIVE_PARAMETER_PREFIXES: Final[tuple[str, ...]] = ("XIAOWEI_STARROCKS_",)
_LIVE_PARAMETERS: Final[frozenset[str]] = frozenset(
    {"XIAOWEI_FEISHU_TENANT_KEY", "XIAOWEI_FEISHU_BOT_OPEN_ID"}
)
_WEB_ONLY: Final[frozenset[str]] = frozenset(
    {"XIAOWEI_WEB_APP_ENABLED", "XIAOWEI_WEB_MODE", "XIAOWEI_WEB_PUBLIC_ORIGIN"}
)
_WEB_TARGET_PORT: Final[int] = 8080
_WILDCARD_HOSTS: Final[frozenset[str]] = frozenset({"", "0.0.0.0", "::", "[::]"})  # noqa: S104

_IMAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}"
)
_SOURCE_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")
_EVIDENCE_REF_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_SOURCE_LABEL: Final[str] = "io.xiaowei.release.source-sha"
_EDGE_LABEL: Final[str] = "io.xiaowei.release.edge-evidence"

SERVICE_SET_INVALID: Final[str] = "service_set_invalid"
IMAGE_NOT_DIGEST_PINNED: Final[str] = "image_not_digest_pinned"
BUILD_PRESENT: Final[str] = "build_present"
RUNTIME_ENVIRONMENT_INVALID: Final[str] = "runtime_environment_invalid"
LIVE_SWITCH_ENABLED: Final[str] = "live_switch_enabled"
WEB_PROFILE_INVALID: Final[str] = "web_profile_invalid"
PORT_SURFACE_INVALID: Final[str] = "port_surface_invalid"
MOUNT_SURFACE_INVALID: Final[str] = "mount_surface_invalid"
SECRET_SET_INVALID: Final[str] = "secret_set_invalid"  # noqa: S105 -- 闭集错误码
CONTAINER_HARDENING_INVALID: Final[str] = "container_hardening_invalid"
COMMAND_INVALID: Final[str] = "command_invalid"
RELEASE_VARIABLE_LEAKED: Final[str] = "release_variable_leaked"
RELEASE_LABEL_INVALID: Final[str] = "release_label_invalid"

VIOLATION_CODES: Final[frozenset[str]] = frozenset(
    {
        SERVICE_SET_INVALID,
        IMAGE_NOT_DIGEST_PINNED,
        BUILD_PRESENT,
        RUNTIME_ENVIRONMENT_INVALID,
        LIVE_SWITCH_ENABLED,
        WEB_PROFILE_INVALID,
        PORT_SURFACE_INVALID,
        MOUNT_SURFACE_INVALID,
        SECRET_SET_INVALID,
        CONTAINER_HARDENING_INVALID,
        COMMAND_INVALID,
        RELEASE_VARIABLE_LEAKED,
        RELEASE_LABEL_INVALID,
    }
)

OK: Final[str] = "ok"
USAGE_INVALID: Final[str] = "usage_invalid"
RELEASE_FILE_SET_INVALID: Final[str] = "release_file_set_invalid"
RELEASE_ENV_TEMPLATE_INVALID: Final[str] = "release_env_template_invalid"
SHELL_ENVIRONMENT_INVALID: Final[str] = "shell_environment_invalid"
RELEASE_RENDER_FAILED: Final[str] = "release_render_failed"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _environment(service: Mapping[str, object]) -> dict[str, str]:
    raw = service.get("environment")
    if isinstance(raw, Mapping):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    return {}


def _check_image(services: Mapping[str, Mapping[str, object]]) -> set[str]:
    images = {services[name].get("image") for name in APP_SERVICES & services.keys()}
    if len(images) != 1:
        return {IMAGE_NOT_DIGEST_PINNED}
    image = images.pop()
    if not isinstance(image, str) or _IMAGE_RE.fullmatch(image) is None:
        return {IMAGE_NOT_DIGEST_PINNED}
    return set()


def _check_environment(name: str, environment: Mapping[str, str]) -> set[str]:
    violations: set[str] = set()
    if any(environment.get(key) != value for key, value in _FIXED_ENVIRONMENT.items()):
        violations.add(RUNTIME_ENVIRONMENT_INVALID)
    for key in _REQUIRED_SWITCHES[name]:
        if environment.get(key) != "false":
            violations.add(LIVE_SWITCH_ENABLED)
    for key in _LIVE_SWITCHES & environment.keys():
        if environment[key] != "false":
            violations.add(LIVE_SWITCH_ENABLED)
    for key in environment:
        if key in _LIVE_PARAMETERS or (
            key.startswith(_LIVE_PARAMETER_PREFIXES)
            and key != "XIAOWEI_STARROCKS_ADAPTER_MODE"
        ):
            violations.add(LIVE_SWITCH_ENABLED)
        if key.startswith("XIAOWEI_RELEASE_"):
            violations.add(RELEASE_VARIABLE_LEAKED)
    if name == "web-app":
        origin = environment.get("XIAOWEI_WEB_PUBLIC_ORIGIN", "")
        if (
            environment.get("XIAOWEI_WEB_APP_ENABLED") != "true"
            or environment.get("XIAOWEI_WEB_MODE") != "https"
            or not origin.startswith("https://")
        ):
            violations.add(WEB_PROFILE_INVALID)
    elif _WEB_ONLY & environment.keys():
        violations.add(WEB_PROFILE_INVALID)
    return violations


def _check_ports(services: Mapping[str, Mapping[str, object]]) -> set[str]:
    for name, service in services.items():
        ports = service.get("ports") or []
        if not isinstance(ports, list):
            return {PORT_SURFACE_INVALID}
        if name != "web-app":
            if ports:
                return {PORT_SURFACE_INVALID}
            continue
        if len(ports) != 1:
            return {PORT_SURFACE_INVALID}
        port = _mapping(ports[0])
        host_ip = port.get("host_ip")
        if (
            port.get("target") != _WEB_TARGET_PORT
            or str(port.get("published")) != str(_WEB_TARGET_PORT)
            or port.get("protocol", "tcp") != "tcp"
            or not isinstance(host_ip, str)
            or host_ip in _WILDCARD_HOSTS
        ):
            return {PORT_SURFACE_INVALID}
    return set()


def _check_mounts(name: str, service: Mapping[str, object]) -> set[str]:
    volumes = service.get("volumes") or []
    if not isinstance(volumes, list):
        return {MOUNT_SURFACE_INVALID}
    if name == "postgres":
        targets = [_mapping(volume).get("target") for volume in volumes]
        types = [_mapping(volume).get("type") for volume in volumes]
        if targets != [_POSTGRES_DATA_TARGET] or types != ["volume"]:
            return {MOUNT_SURFACE_INVALID}
        return set()
    mounted: dict[str, bool] = {}
    for volume in volumes:
        mount = _mapping(volume)
        target = mount.get("target")
        if mount.get("type") != "bind" or not isinstance(target, str):
            return {MOUNT_SURFACE_INVALID}
        prefix = f"{_CONFIG_TARGET}/"
        domain = target.removeprefix(prefix)
        if not target.startswith(prefix) or "/" in domain or domain in mounted:
            return {MOUNT_SURFACE_INVALID}
        mounted[domain] = mount.get("read_only") is True
    if mounted != dict(_CONFIG_MATRIX[name]):
        return {MOUNT_SURFACE_INVALID}
    return set()


def _check_secrets(
    model: Mapping[str, object], services: Mapping[str, Mapping[str, object]]
) -> set[str]:
    if set(_mapping(model.get("secrets"))) != {_POSTGRES_SECRET}:
        return {SECRET_SET_INVALID}
    for service in services.values():
        sources = [_mapping(item).get("source") for item in service.get("secrets") or []]  # type: ignore[union-attr]
        if sources != [_POSTGRES_SECRET]:
            return {SECRET_SET_INVALID}
    return set()


def _check_hardening(name: str, service: Mapping[str, object]) -> set[str]:
    if (
        service.get("privileged") is True
        or service.get("network_mode") not in (None, "")
        or service.get("pid") not in (None, "")
        or service.get("ipc") not in (None, "")
        or service.get("cap_add")
        or service.get("devices")
        or service.get("extra_hosts")
    ):
        return {CONTAINER_HARDENING_INVALID}
    if name in APP_SERVICES and (
        service.get("read_only") is not True
        or service.get("cap_drop") != ["ALL"]
        or service.get("security_opt") != ["no-new-privileges:true"]
        or service.get("init") is not True
        or service.get("user") not in (None, "")
    ):
        return {CONTAINER_HARDENING_INVALID}
    return set()


def _check_labels(service: Mapping[str, object]) -> set[str]:
    labels = _mapping(service.get("labels"))
    source = labels.get(_SOURCE_LABEL)
    edge = labels.get(_EDGE_LABEL)
    if (
        not isinstance(source, str)
        or _SOURCE_SHA_RE.fullmatch(source) is None
        or not isinstance(edge, str)
        or _EVIDENCE_REF_RE.fullmatch(edge) is None
    ):
        return {RELEASE_LABEL_INVALID}
    return set()


def release_violations(model: Mapping[str, object]) -> tuple[str, ...]:
    """核对合成后的最终模型；返回排序后的闭集违例码，空元组表示合法。

    接受默认服务集合或"默认 + 渠道 profile"两种渲染；渠道服务在场时同样逐项核对，
    证明它们万一被显式拉起也只会以 release 形态 fail-closed。
    """
    services = {
        str(name): _mapping(service)
        for name, service in _mapping(model.get("services")).items()
    }
    violations: set[str] = set()
    if set(services) not in (set(DEFAULT_SERVICES), set(DEFAULT_SERVICES | CHANNEL_SERVICES)):
        violations.add(SERVICE_SET_INVALID)
    violations |= _check_image(services)
    violations |= _check_ports(services)
    violations |= _check_secrets(model, services)
    for name, service in services.items():
        if name not in APP_SERVICES and name != "postgres":
            continue
        violations |= _check_mounts(name, service)
        violations |= _check_hardening(name, service)
        if name == "postgres":
            continue
        if "build" in service:
            violations.add(BUILD_PRESENT)
        if service.get("command") != ["python", "-m", _COMMANDS[name]]:
            violations.add(COMMAND_INVALID)
        violations |= _check_environment(name, _environment(service))
        violations |= _check_labels(service)
    return tuple(sorted(violations))


def _read_template(path: Path) -> dict[str, str] | None:
    """只解析 ``KEY=VALUE`` 行；任何值都不离开本函数的调用方。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key in values:
            return None
        values[key] = value.strip()
    return values


def _parse_arguments(argv: Sequence[str]) -> tuple[str, tuple[str, ...]] | None:
    env_file: str | None = None
    files: list[str] = []
    iterator = iter(argv)
    for argument in iterator:
        value = next(iterator, None)
        if value is None:
            return None
        if argument == "--env-file" and env_file is None:
            env_file = value
        elif argument == "-f":
            files.append(value)
        else:
            return None
    if env_file is None:
        return None
    return env_file, tuple(files)


def _render(
    *,
    compose_command: Sequence[str],
    env_file: Path,
    environ: Mapping[str, str],
    profiles: bool,
) -> Mapping[str, object] | None:
    argv = [*compose_command, "--env-file", str(env_file)]
    if profiles:
        argv.extend(("--profile", _CHANNEL_PROFILE))
    for name in RELEASE_COMPOSE_FILES:
        argv.extend(("-f", str(_ROOT / name)))
    argv.extend(("config", "--format", "json"))
    try:
        result = subprocess.run(  # noqa: S603 -- argv[0] 由 shutil.which 解析
            argv,
            cwd=_ROOT,
            env=dict(environ),
            check=True,
            capture_output=True,
            text=True,
            timeout=60.0,
        )
        parsed = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _default_compose_command() -> tuple[str, ...] | None:
    from scripts.compose_smoke import (
        SmokeError,
        _default_runner,
        _resolve_compose_command,
    )

    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        return _resolve_compose_command(docker=docker, runner=_default_runner)
    except SmokeError:
        return None


def check(
    argv: Sequence[str], *, environ: Mapping[str, str]
) -> tuple[str, ...] | None:
    """返回闭集结果码；``None`` 表示调用形态非法。"""
    parsed = _parse_arguments(argv)
    if parsed is None:
        return None
    env_file, files = parsed
    if files and files != RELEASE_COMPOSE_FILES:
        return (RELEASE_FILE_SET_INVALID,)
    if any(key.startswith(("XIAOWEI_", "COMPOSE_")) for key in environ):
        return (SHELL_ENVIRONMENT_INVALID,)
    template = _read_template(Path(env_file))
    if (
        template is None
        or set(template) != RELEASE_TEMPLATE_KEYS
        or not all(template.values())
    ):
        return (RELEASE_ENV_TEMPLATE_INVALID,)
    compose_command = _default_compose_command()
    if compose_command is None:
        return (RELEASE_RENDER_FAILED,)
    default = _render(
        compose_command=compose_command,
        env_file=Path(env_file),
        environ=environ,
        profiles=False,
    )
    full = _render(
        compose_command=compose_command,
        env_file=Path(env_file),
        environ=environ,
        profiles=True,
    )
    if default is None or full is None:
        return (RELEASE_RENDER_FAILED,)
    violations = set(release_violations(full))
    if set(_mapping(default.get("services"))) != DEFAULT_SERVICES:
        violations.add(SERVICE_SET_INVALID)
    return tuple(sorted(violations)) or (OK,)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """0 为唯一合法 release；1 为任一检查未通过；2 为调用形态非法。"""
    import os

    result = check(
        sys.argv[1:] if argv is None else argv,
        environ=os.environ if environ is None else environ,
    )
    if result is None:
        stderr.write(f"{_COMMAND}: {USAGE_INVALID}\n")
        return 2
    stdout.write(f"{_COMMAND}: {','.join(result)}\n")
    return 0 if result == (OK,) else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "RELEASE_COMPOSE_FILES",
    "RELEASE_TEMPLATE_KEYS",
    "VIOLATION_CODES",
    "check",
    "main",
    "release_violations",
]

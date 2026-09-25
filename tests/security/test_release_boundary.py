"""W5 provider-off release 的边界：任何真实调用开关被打开都必须 fail-closed。

两层各自承重，互不代替：

1. **进程层**：``Settings`` 在 ``runtime_profile=release`` 下拒绝任何被打开的真实调用
   开关。即使有人绕过 Compose 契约（手工 ``docker run``、额外 override、改 release 文件），
   进程也起不来。
2. **部署层**：``scripts.release_compose`` 在 ``up`` 之前检查合成后的最终 Compose 模型；
   任何额外 override 试图打开 Gemini、OAuth、listener、channel-worker、StarRocks、真实测试
   开关，或改动端口面、镜像、挂载矩阵，都得到闭集失败码，而不是被当作本阶段合法组合。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import release_compose
from scripts.compose_smoke import _default_runner, _resolve_compose_command

from xiaowei_agent.config import ConfigError, load_settings

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_ENV = {
    "XIAOWEI_ENVIRONMENT_ID": "dev",
    "XIAOWEI_RUNTIME_PROFILE": "release",
    "XIAOWEI_STARROCKS_ADAPTER_MODE": "disabled",
}
_LIVE_SWITCHES = (
    "XIAOWEI_GEMINI_ENABLED",
    "XIAOWEI_FEISHU_OAUTH_ENABLED",
    "XIAOWEI_FEISHU_LISTENER_ENABLED",
    "XIAOWEI_CHANNEL_WORKER_ENABLED",
    "XIAOWEI_GEMINI_REAL_TEST_ENABLED",
    "XIAOWEI_FEISHU_REAL_TEST_ENABLED",
)
_IMAGE = "registry.example.invalid/xiaowei-agent@sha256:" + "cd" * 32
_TEMPLATE = {
    "XIAOWEI_RELEASE_IMAGE": _IMAGE,
    "XIAOWEI_RELEASE_ACTOR": "release-admin",
    "XIAOWEI_RELEASE_SOURCE_SHA": "0" * 40,
    "XIAOWEI_RELEASE_EDGE_EVIDENCE_REF": "edge-evidence-2026-09",
    "XIAOWEI_RELEASE_WEB_BIND_IP": "127.0.0.1",
    "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://sso.example.invalid",
}


# --------------------------------------------------------------------------
# 进程层
# --------------------------------------------------------------------------


# 每个开关配齐它自己的完整 profile：拒绝必须来自 release 规则，而不是"配置不全"。
_COMPLETE_PROFILES: dict[str, dict[str, str]] = {
    "XIAOWEI_GEMINI_ENABLED": {},
    "XIAOWEI_FEISHU_OAUTH_ENABLED": {
        "XIAOWEI_WEB_APP_ENABLED": "true",
        "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://sso.example.invalid",
    },
    "XIAOWEI_FEISHU_LISTENER_ENABLED": {
        "XIAOWEI_FEISHU_TENANT_KEY": "tenant-key",
        "XIAOWEI_FEISHU_BOT_OPEN_ID": "ou_" + "bot",
    },
    "XIAOWEI_CHANNEL_WORKER_ENABLED": {
        "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://sso.example.invalid",
    },
    "XIAOWEI_GEMINI_REAL_TEST_ENABLED": {},
    "XIAOWEI_FEISHU_REAL_TEST_ENABLED": {},
}


@pytest.mark.parametrize("switch", _LIVE_SWITCHES)
def test_complete_profiles_load_outside_release(switch: str) -> None:
    """对照组：同一份完整 profile 在 offline 形态下能加载，证明下一条的拒绝来自 release。"""
    settings = load_settings(
        {"XIAOWEI_ENVIRONMENT_ID": "dev", switch: "true", **_COMPLETE_PROFILES[switch]}
    )
    assert settings.runtime_profile.value == "offline_recording"


@pytest.mark.parametrize("switch", _LIVE_SWITCHES)
def test_release_settings_reject_every_live_switch(switch: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({**_RELEASE_ENV, switch: "true", **_COMPLETE_PROFILES[switch]})


@pytest.mark.parametrize("switch", _LIVE_SWITCHES)
def test_release_settings_accept_the_switch_when_it_is_off(switch: str) -> None:
    settings = load_settings({**_RELEASE_ENV, switch: "false"})
    assert settings.runtime_profile.value == "release"


def test_offline_profile_keeps_the_existing_opt_in_switches() -> None:
    """release 的收紧不外溢：offline_recording 下既有开关语义不变。"""
    settings = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_GEMINI_ENABLED": "true"})
    assert settings.gemini_enabled is True


def test_release_web_app_still_loads_with_only_a_public_origin() -> None:
    settings = load_settings(
        {
            **_RELEASE_ENV,
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://sso.example.invalid",
        }
    )
    assert settings.web_app_enabled is True
    assert settings.feishu_oauth_enabled is False


# --------------------------------------------------------------------------
# 部署层：额外 override 的负例矩阵
# --------------------------------------------------------------------------


def _render(extra: dict[str, Any] | None, tmp_path: Path) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable; compose-smoke remains the no-skip gate")
    command = [
        *_resolve_compose_command(docker=docker, runner=_default_runner),
        "--profile",
        "m7-channels",
        "-f",
        str(_ROOT / "docker-compose.yml"),
        "-f",
        str(_ROOT / "docker-compose.release.yml"),
    ]
    if extra is not None:
        override = tmp_path / "extra-override.json"
        override.write_text(json.dumps(extra), encoding="utf-8")
        command.extend(("-f", str(override)))
    command.extend(("config", "--format", "json"))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XIAOWEI_", "COMPOSE_"))
    }
    environment.update(_TEMPLATE)
    result = subprocess.run(  # noqa: S603 -- binary 由 shutil.which 解析
        command,
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _env(service: str, **values: str) -> dict[str, Any]:
    return {"services": {service: {"environment": values}}}


_NEGATIVE_OVERRIDES: list[tuple[str, dict[str, Any], str]] = [
    # 真实调用开关
    ("gemini", _env("worker", XIAOWEI_GEMINI_ENABLED="true"), "live_switch_enabled"),
    ("oauth", _env("web-app", XIAOWEI_FEISHU_OAUTH_ENABLED="true"), "live_switch_enabled"),
    (
        "listener",
        _env("feishu-listener", XIAOWEI_FEISHU_LISTENER_ENABLED="true"),
        "live_switch_enabled",
    ),
    (
        "channel-worker",
        _env("channel-worker", XIAOWEI_CHANNEL_WORKER_ENABLED="true"),
        "live_switch_enabled",
    ),
    (
        "gemini-real-test",
        _env("web-app", XIAOWEI_GEMINI_REAL_TEST_ENABLED="true"),
        "live_switch_enabled",
    ),
    (
        "feishu-real-test",
        _env("web-app", XIAOWEI_FEISHU_REAL_TEST_ENABLED="true"),
        "live_switch_enabled",
    ),
    (
        "listener-flag-on-api",
        _env("api", XIAOWEI_FEISHU_LISTENER_ENABLED="true"),
        "live_switch_enabled",
    ),
    (
        "feishu-tenant",
        _env("feishu-listener", XIAOWEI_FEISHU_TENANT_KEY="tenant"),
        "live_switch_enabled",
    ),
    (
        "starrocks-host",
        _env("worker", XIAOWEI_STARROCKS_HOST="sr.example.invalid"),
        "live_switch_enabled",
    ),
    # 运行形态与作用域
    (
        "starrocks-recording",
        _env("worker", XIAOWEI_STARROCKS_ADAPTER_MODE="recording"),
        "runtime_environment_invalid",
    ),
    (
        "starrocks-test-readonly",
        _env("api", XIAOWEI_STARROCKS_ADAPTER_MODE="test_readonly"),
        "runtime_environment_invalid",
    ),
    (
        "offline-profile",
        _env("worker", XIAOWEI_RUNTIME_PROFILE="offline_recording"),
        "runtime_environment_invalid",
    ),
    (
        "scope",
        _env("web-app", XIAOWEI_ENVIRONMENT_ID="test"),
        "runtime_environment_invalid",
    ),
    (
        "smoke-barrier",
        _env("worker", XIAOWEI_SMOKE_STEP_BARRIER="true"),
        "runtime_environment_invalid",
    ),
    ("web-lan-http", _env("web-app", XIAOWEI_WEB_MODE="lan_http"), "web_profile_invalid"),
    ("web-disabled", _env("web-app", XIAOWEI_WEB_APP_ENABLED="false"), "web_profile_invalid"),
    (
        "web-flag-on-worker",
        _env("worker", XIAOWEI_WEB_APP_ENABLED="true"),
        "web_profile_invalid",
    ),
    # 端口面
    (
        "api-port",
        {"services": {"api": {"ports": ["127.0.0.1:8000:8000"]}}},
        "port_surface_invalid",
    ),
    (
        "postgres-port",
        {"services": {"postgres": {"ports": ["127.0.0.1:5432:5432"]}}},
        "port_surface_invalid",
    ),
    (
        "worker-port",
        {"services": {"worker": {"ports": ["127.0.0.1:9000:9000"]}}},
        "port_surface_invalid",
    ),
    (
        "second-web-port",
        {"services": {"web-app": {"ports": ["127.0.0.1:8443:8080"]}}},
        "port_surface_invalid",
    ),
    # 镜像
    (
        "mutable-tag",
        {"services": {"worker": {"image": "xiaowei-agent:latest"}}},
        "image_not_digest_pinned",
    ),
    (
        "second-digest",
        {
            "services": {
                "worker": {
                    "image": "registry.example.invalid/other@sha256:" + "ef" * 32
                }
            }
        },
        "image_not_digest_pinned",
    ),
    (
        "rebuild",
        {"services": {"api": {"build": {"context": "."}}}},
        "build_present",
    ),
    # 挂载与凭据
    (
        "worker-config-writable",
        {
            "services": {
                "worker": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "./.config/ai",
                            "target": "/run/xiaowei-config/ai",
                            "read_only": False,
                        }
                    ]
                }
            }
        },
        "mount_surface_invalid",
    ),
    (
        "api-config-mount",
        {
            "services": {
                "api": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "./.config/feishu",
                            "target": "/run/xiaowei-config/feishu",
                            "read_only": True,
                        }
                    ]
                }
            }
        },
        "mount_surface_invalid",
    ),
    (
        "legacy-identity",
        {
            "services": {
                "web-app": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "./.config/ai",
                            "target": "/run/xiaowei-legacy/feishu-identities.json",
                            "read_only": True,
                        }
                    ]
                }
            }
        },
        "mount_surface_invalid",
    ),
    (
        "docker-socket",
        {"services": {"worker": {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}}},
        "mount_surface_invalid",
    ),
    (
        "extra-secret",
        {
            "services": {"worker": {"secrets": ["postgres_password", "extra"]}},
            "secrets": {"extra": {"file": "./README.md"}},
        },
        "secret_set_invalid",
    ),
    # 容器加固与命令
    ("privileged", {"services": {"worker": {"privileged": True}}}, "container_hardening_invalid"),
    (
        "host-network",
        {"services": {"web-app": {"network_mode": "host"}}},
        "container_hardening_invalid",
    ),
    (
        "writable-rootfs",
        {"services": {"api": {"read_only": False}}},
        "container_hardening_invalid",
    ),
    (
        "command",
        {"services": {"worker": {"command": ["python", "-m", "xiaowei_agent.interfaces.api"]}}},
        "command_invalid",
    ),
    # 部署模板变量不进容器
    (
        "template-leak",
        _env("api", XIAOWEI_RELEASE_IMAGE=_IMAGE),
        "release_variable_leaked",
    ),
    # 服务面
    (
        "extra-service",
        {"services": {"sidecar": {"image": "busybox@sha256:" + "12" * 32}}},
        "service_set_invalid",
    ),
    # 发布证据标签
    (
        "source-label",
        {"services": {"api": {"labels": {"io.xiaowei.release.source-sha": "main"}}}},
        "release_label_invalid",
    ),
]


def test_the_unmodified_release_model_has_no_violation(tmp_path: Path) -> None:
    assert release_compose.release_violations(_render(None, tmp_path)) == ()


@pytest.mark.parametrize(
    ("override", "code"),
    [(override, code) for _, override, code in _NEGATIVE_OVERRIDES],
    ids=[name for name, _, _ in _NEGATIVE_OVERRIDES],
)
def test_any_extra_override_that_widens_the_release_is_rejected(
    tmp_path: Path, override: dict[str, Any], code: str
) -> None:
    violations = release_compose.release_violations(_render(override, tmp_path))
    assert code in violations
    assert set(violations) <= release_compose.VIOLATION_CODES


def test_violation_codes_are_a_closed_set_and_every_one_is_exercised() -> None:
    exercised = {code for _, _, code in _NEGATIVE_OVERRIDES}
    assert exercised == release_compose.VIOLATION_CODES


def test_release_check_is_not_shipped_in_the_runtime_package() -> None:
    """检查器属于部署工具面，不进 wheel/镜像，也不给应用进程一个"自证合规"的入口。"""
    assert not (_ROOT / "src/xiaowei_agent/release_compose.py").exists()
    assert (_ROOT / "scripts/release_compose.py").is_file()

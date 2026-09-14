"""Dockerfile 与 Compose 的静态安全、装配和 override 契约。"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from scripts.compose_smoke import _default_runner, _resolve_compose_command

from xiaowei_agent import interfaces as interfaces_module
from xiaowei_agent.capabilities.target import KNOWN_ENVIRONMENT_IDS
from xiaowei_agent.interfaces import feishu_oauth as feishu_oauth_module

_ROOT = Path(__file__).resolve().parents[2]
_PROCESS_SERVICES = {
    "api",
    "worker",
    "feishu-listener",
    "channel-worker",
    "web-app",
}
_APP_SERVICES = _PROCESS_SERVICES | {"migrate"}
_CHANNEL_SERVICES = {"feishu-listener", "channel-worker", "web-app"}
_NON_CHANNEL_APP_SERVICES = _APP_SERVICES - _CHANNEL_SERVICES
_FEISHU_LIVE_ENVIRONMENT = {
    "XIAOWEI_FEISHU_APP_ID",
    "XIAOWEI_FEISHU_APP_SECRET_FILE",
    "XIAOWEI_FEISHU_TENANT_KEY",
    "XIAOWEI_FEISHU_BOT_OPEN_ID",
    "XIAOWEI_FEISHU_IDENTITY_FILE",
    "XIAOWEI_WEB_PUBLIC_ORIGIN",
}
_CHANNEL_ONLY_ENVIRONMENT = _FEISHU_LIVE_ENVIRONMENT | {
    "XIAOWEI_FEISHU_LISTENER_ENABLED",
    "XIAOWEI_CHANNEL_WORKER_ENABLED",
    "XIAOWEI_WEB_APP_ENABLED",
    "XIAOWEI_FEISHU_OAUTH_ENABLED",
    "XIAOWEI_WEB_BIND_HOST",
    "XIAOWEI_WEB_BIND_PORT",
}


def _yaml(name: str) -> dict[str, Any]:
    value = yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_base_compose_has_the_complete_single_image_topology() -> None:
    compose = _yaml("docker-compose.yml")
    services = compose["services"]
    assert set(services) == _APP_SERVICES | {"postgres"}
    images = {services[name]["image"] for name in _APP_SERVICES}
    assert images == {"xiaowei-agent:${COMPOSE_XIAOWEI_IMAGE_TAG:-m5-local}"}
    assert "latest" not in images.pop()
    assert "COMPOSE_XIAOWEI_IMAGE_TAG" not in (
        _ROOT / ".env.example"
    ).read_text(encoding="utf-8")
    assert {services[name]["build"]["dockerfile"] for name in _APP_SERVICES} == {
        "Dockerfile"
    }
    assert services["migrate"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"}
    }
    for name in _PROCESS_SERVICES:
        assert services[name]["depends_on"] == {
            "migrate": {"condition": "service_completed_successfully"}
        }


def test_model_override_is_worker_only_and_retains_postgres_secret() -> None:
    base = _yaml("docker-compose.yml")
    override = _yaml("docker-compose.model.yml")
    assert "gemini_api_key" not in base["secrets"]
    assert all(
        "gemini_api_key" not in service.get("secrets", ())
        for service in base["services"].values()
    )
    assert "XIAOWEI_GEMINI_ENABLED" not in base["x-app-environment"]
    assert override == {
        "services": {
            "worker": {
                "environment": {"XIAOWEI_GEMINI_ENABLED": "true"},
                "secrets": ["postgres_password", "gemini_api_key"],
            }
        },
        "secrets": {
            "gemini_api_key": {
                "file": "./.secrets/gemini_api_key"
            }
        },
    }


def test_rendered_model_config_keeps_key_out_of_environments_and_non_worker_mounts() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable; compose-smoke remains the no-skip gate")
    command = _resolve_compose_command(docker=docker, runner=_default_runner)
    environment = dict(os.environ)
    environment.pop("GEMINI_API_KEY", None)
    environment["GEMINI_API_KEY_FILE"] = str(
        _ROOT / ".secrets" / "must-not-override-gemini-path"
    )
    result = subprocess.run(  # noqa: S603 -- binary 由 shutil.which 解析
        [
            *command,
            "-f",
            str(_ROOT / "docker-compose.yml"),
            "-f",
            str(_ROOT / "docker-compose.model.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(result.stdout)
    secret = rendered["secrets"]["gemini_api_key"]
    assert secret["file"] == str(_ROOT / ".secrets" / "gemini_api_key")
    assert "environment" not in secret
    services = rendered["services"]
    assert services["worker"]["environment"]["XIAOWEI_GEMINI_ENABLED"] == "true"
    assert {item["source"] for item in services["worker"]["secrets"]} == {
        "postgres_password",
        "gemini_api_key",
    }
    for name, service in services.items():
        if name == "worker":
            continue
        assert "XIAOWEI_GEMINI_ENABLED" not in service.get("environment", {})
        assert "gemini_api_key" not in {
            item["source"] for item in service.get("secrets", ())
        }
    assert all(
        not any(
            item.startswith(("GEMINI_API_KEY=", "GEMINI_API_KEY_FILE="))
            for item in service.get("environment", ())
        )
        for service in services.values()
    )


def test_local_environment_exists_in_the_registered_target_directory() -> None:
    services = _yaml("docker-compose.yml")["services"]
    environments = {
        services[name]["environment"]["XIAOWEI_ENVIRONMENT_ID"]
        for name in _APP_SERVICES
    }
    assert len(environments) == 1
    environment_id = environments.pop()
    assert environment_id in KNOWN_ENVIRONMENT_IDS
    assert f"XIAOWEI_ENVIRONMENT_ID={environment_id}" in (
        _ROOT / ".env.example"
    ).read_text(encoding="utf-8").splitlines()


def test_compose_exposes_only_http_apps_on_distinct_host_loopback_ports() -> None:
    services = _yaml("docker-compose.yml")["services"]
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]
    assert services["web-app"]["ports"] == ["127.0.0.1:8080:8080"]
    for name in {"postgres", "worker", "migrate", "feishu-listener", "channel-worker"}:
        assert "ports" not in services[name]
    for service in services.values():
        assert "container_name" not in service
        assert service.get("network_mode") != "host"
        assert service.get("privileged") is not True
        assert "/var/run/docker.sock" not in str(service.get("volumes", ()))


def test_compose_resources_remain_project_scoped_named_resources() -> None:
    compose = _yaml("docker-compose.yml")
    for section in ("volumes", "networks"):
        resources = compose.get(section, {})
        assert isinstance(resources, dict)
        for definition in resources.values():
            options = definition or {}
            assert isinstance(options, dict)
            assert "name" not in options
            assert options.get("external") is not True

    mount = compose["services"]["postgres"]["volumes"]
    assert len(mount) == 1
    assert isinstance(mount[0], str)
    source, target = mount[0].split(":", maxsplit=1)
    assert target == "/var/lib/postgresql/data"
    assert source in compose["volumes"]
    assert not source.startswith((".", "/"))


def test_secrets_are_file_references_and_never_environment_values() -> None:
    compose = _yaml("docker-compose.yml")
    assert compose["secrets"] == {
        "postgres_password": {"file": "./.secrets/postgres_password"},
        "feishu_app_secret": {"file": "./.secrets/feishu_app_secret"},
    }
    services = compose["services"]
    for service in services.values():
        environment = service.get("environment", {})
        password_keys = {key for key in environment if "PASSWORD" in key}
        assert all(key.endswith("_FILE") for key in password_keys)
        if "POSTGRES_PASSWORD_FILE" in environment:
            assert environment["POSTGRES_PASSWORD_FILE"] == (
                "/run/secrets/postgres_" + "password"
            )
    for name in _CHANNEL_SERVICES:
        assert services[name]["secrets"] == [
            "postgres_password",
            "feishu_app_secret",
        ]
    for name in _NON_CHANNEL_APP_SERVICES:
        assert services[name]["secrets"] == ["postgres_password"]
    assert services["postgres"]["secrets"] == ["postgres_password"]
    assert not (_ROOT / ".secrets/postgres_password").exists()
    assert not (_ROOT / ".secrets/feishu_app_secret").exists()


def test_feishu_identity_bind_is_read_only_and_scoped_to_its_consumers() -> None:
    compose = _yaml("docker-compose.yml")
    services = compose["services"]
    assert "configs" not in compose
    expected = {
        "type": "bind",
        "source": "./.secrets/feishu-identities.json",
        "target": "/run/config/feishu-identities.json",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    assert services["feishu-listener"]["volumes"] == [expected]
    assert services["web-app"]["volumes"] == [expected]
    for name in (_APP_SERVICES | {"postgres"}) - {"feishu-listener", "web-app"}:
        assert expected not in services[name].get("volumes", [])
    assert not (_ROOT / ".secrets/feishu-identities.json").exists()


def test_app_services_are_read_only_unprivileged_and_use_exec_commands() -> None:
    services = _yaml("docker-compose.yml")["services"]
    expected_modules = {
        "api": "xiaowei_agent.interfaces.api",
        "worker": "xiaowei_agent.interfaces.worker",
        "migrate": "xiaowei_agent.interfaces.migrate",
        "feishu-listener": "xiaowei_agent.interfaces.feishu_listener",
        "channel-worker": "xiaowei_agent.interfaces.feishu_worker",
        "web-app": "xiaowei_agent.interfaces.web_app",
    }
    for name, module in expected_modules.items():
        service = services[name]
        assert service["command"] == ["python", "-m", module]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["init"] is True


def test_channel_processes_are_profile_gated_and_default_fail_closed() -> None:
    compose = _yaml("docker-compose.yml")
    services = compose["services"]
    expected_flags = {
        "feishu-listener": {"XIAOWEI_FEISHU_LISTENER_ENABLED": "false"},
        "channel-worker": {"XIAOWEI_CHANNEL_WORKER_ENABLED": "false"},
        "web-app": {
            "XIAOWEI_WEB_APP_ENABLED": "false",
            "XIAOWEI_FEISHU_OAUTH_ENABLED": "false",
        },
    }
    for name in _CHANNEL_SERVICES:
        assert services[name]["profiles"] == ["m7-channels"]
        environment = services[name]["environment"]
        assert expected_flags[name].items() <= environment.items()
        assert not (_FEISHU_LIVE_ENVIRONMENT & environment.keys())

    assert not (_CHANNEL_ONLY_ENVIRONMENT & compose["x-app-environment"].keys())
    for name in _NON_CHANNEL_APP_SERVICES | {"postgres"}:
        assert not (_CHANNEL_ONLY_ENVIRONMENT & services[name]["environment"].keys())

    assert services["web-app"]["environment"]["XIAOWEI_WEB_BIND_HOST"] == (
        "0.0." + "0.0"
    )
    assert services["web-app"]["environment"]["XIAOWEI_WEB_BIND_PORT"] == "8080"


def test_compose_healthchecks_use_available_binaries_and_no_shell() -> None:
    services = _yaml("docker-compose.yml")["services"]
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD"
    api_test = services["api"]["healthcheck"]["test"]
    assert api_test[:3] == ["CMD", "python", "-c"]
    assert "http.client.HTTPConnection('127.0.0.1',8000,timeout=2)" in api_test[3]
    assert "request('GET','/healthz')" in api_test[3]
    assert "status == 200" in api_test[3]
    web_test = services["web-app"]["healthcheck"]["test"]
    assert web_test[:3] == ["CMD", "python", "-c"]
    assert "http.client.HTTPConnection('127.0.0.1',8080,timeout=2)" in web_test[3]
    assert "request('GET','/healthz')" in web_test[3]
    assert "status == 200" in web_test[3]
    assert "urllib" not in api_test[3]
    assert "urllib" not in web_test[3]
    assert "curl" not in str(services)


def test_overrides_have_only_the_approved_worker_environment_paths() -> None:
    base = _yaml("docker-compose.yml")
    smoke = _yaml("docker-compose.smoke.yml")
    barrier = _yaml("docker-compose.barrier.yml")
    assert set(smoke) == {"services"}
    assert set(smoke["services"]) == {"worker", "web-app"}
    assert set(smoke["services"]["worker"]) == {"environment"}
    assert set(smoke["services"]["worker"]["environment"]) == {
        "XIAOWEI_LEASE_TTL_SECONDS",
        "XIAOWEI_HEARTBEAT_INTERVAL_SECONDS",
        "XIAOWEI_WORKER_POLL_INTERVAL_SECONDS",
        "XIAOWEI_DB_CONNECT_TIMEOUT_SECONDS",
        "XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS",
    }
    web = smoke["services"]["web-app"]
    assert set(web) == {"environment", "extra_hosts"}
    assert web["environment"] == {
        "XIAOWEI_WEB_APP_ENABLED": "true",
        "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
        "XIAOWEI_FEISHU_APP_ID": "cli_smoke_fake_app",
        "XIAOWEI_FEISHU_APP_SECRET_FILE": "/run/secrets/feishu_app_secret",
        "XIAOWEI_FEISHU_IDENTITY_FILE": "/run/config/feishu-identities.json",
        "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://sso.example.invalid",
    }
    provider_origin = urlsplit(interfaces_module.FEISHU_PROVIDER_ORIGIN)
    assert provider_origin.scheme == "https"
    assert provider_origin.hostname == "open.feishu.cn"
    assert provider_origin.netloc == provider_origin.hostname
    assert provider_origin.port is None
    assert provider_origin.username is None
    assert provider_origin.password is None
    assert not provider_origin.path
    assert not provider_origin.query
    assert not provider_origin.fragment

    endpoints = feishu_oauth_module.FEISHU_OAUTH_ENDPOINT_URLS
    assert len(endpoints) == 3
    endpoint_hosts: set[str] = set()
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        assert f"{parsed.scheme}://{parsed.netloc}" == (
            interfaces_module.FEISHU_PROVIDER_ORIGIN
        )
        assert parsed.hostname is not None
        assert parsed.netloc == parsed.hostname
        assert parsed.port is None
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.path.startswith("/open-apis/")
        assert not parsed.query
        assert not parsed.fragment
        endpoint_hosts.add(parsed.hostname)
    assert endpoint_hosts == {"open.feishu.cn"}

    blackholed_hosts: set[str] = set()
    for entry in web["extra_hosts"]:
        host, separator, address = entry.partition(":")
        assert separator
        if address == "127.0.0.1":
            blackholed_hosts.add(host)
    assert endpoint_hosts <= blackholed_hosts

    for name in _NON_CHANNEL_APP_SERVICES | {"postgres"}:
        merged_environment = dict(base["services"][name].get("environment", {}))
        merged_environment.update(
            smoke["services"].get(name, {}).get("environment", {})
        )
        assert not (_CHANNEL_ONLY_ENVIRONMENT & merged_environment.keys())
    assert barrier == {
        "services": {
            "worker": {
                "environment": {"XIAOWEI_SMOKE_STEP_BARRIER": "true"}
            }
        }
    }


def test_m6b_override_is_explicit_parameterized_and_mounts_file_credentials() -> None:
    override = _yaml("docker-compose.m6b-test.yml")
    assert set(override["services"]) == {"api", "worker"}
    for name in ("api", "worker"):
        service = override["services"][name]
        assert set(service) == {"environment", "secrets"}
        environment = service["environment"]
        assert environment["XIAOWEI_ENVIRONMENT_ID"] == "test"
        assert environment["XIAOWEI_STARROCKS_ADAPTER_MODE"] == "test_readonly"
        assert environment["XIAOWEI_STARROCKS_PASSWORD_FILE"] == (
            "/run/secrets/m6b_starrocks_" + "password"
        )
        assert environment["XIAOWEI_STARROCKS_CA_FILE"] == (
            "/run/secrets/m6b_starrocks_ca"
        )
        parameterized = {
            value
            for key, value in environment.items()
            if key
            not in {
                "XIAOWEI_ENVIRONMENT_ID",
                "XIAOWEI_STARROCKS_ADAPTER_MODE",
                "XIAOWEI_STARROCKS_PASSWORD_FILE",
                "XIAOWEI_STARROCKS_CA_FILE",
            }
        }
        assert parameterized
        assert all(
            isinstance(value, str) and value.startswith("${M6B_") and ":?" in value
            for value in parameterized
        )
        assert service["secrets"] == [
            "postgres_password",
            "m6b_starrocks_password",
            "m6b_starrocks_ca",
        ]
    assert set(override["secrets"]) == {
        "m6b_starrocks_password",
        "m6b_starrocks_ca",
    }
    assert all(
        definition["file"].startswith("${M6B_")
        for definition in override["secrets"].values()
    )


def test_dockerfile_is_reproducible_non_root_and_excludes_tests() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all("@sha256:" in line for line in from_lines)
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "USER xiaowei" in dockerfile
    assert 'CMD ["python", "-m", "xiaowei_agent.interfaces.api"]' in dockerfile
    ignored = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "tests" in ignored
    assert ".git" in ignored
    assert ".secrets" in ignored

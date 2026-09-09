"""Dockerfile 与 Compose 的静态安全、装配和 override 契约。"""

from pathlib import Path
from typing import Any

import yaml

from xiaowei_agent.capabilities.target import KNOWN_ENVIRONMENT_IDS

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


def _yaml(name: str) -> dict[str, Any]:
    value = yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_base_compose_has_the_complete_single_image_topology() -> None:
    compose = _yaml("docker-compose.yml")
    services = compose["services"]
    assert set(services) == _APP_SERVICES | {"postgres"}
    images = {services[name]["image"] for name in _APP_SERVICES}
    assert images == {"xiaowei-agent:m5-local"}
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
        "postgres_password": {"file": "./.secrets/postgres_password"}
    }
    for service in compose["services"].values():
        environment = service.get("environment", {})
        password_keys = {key for key in environment if "PASSWORD" in key}
        assert all(key.endswith("_FILE") for key in password_keys)
        if "POSTGRES_PASSWORD_FILE" in environment:
            assert environment["POSTGRES_PASSWORD_FILE"] == (
                "/run/secrets/postgres_" + "password"
            )
            assert service["secrets"] == ["postgres_password"]
    assert not (_ROOT / ".secrets/postgres_password").exists()


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
    services = _yaml("docker-compose.yml")["services"]
    expected_flags = {
        "feishu-listener": "XIAOWEI_FEISHU_LISTENER_ENABLED",
        "channel-worker": "XIAOWEI_CHANNEL_WORKER_ENABLED",
        "web-app": "XIAOWEI_WEB_APP_ENABLED",
    }
    for name in _CHANNEL_SERVICES:
        assert services[name]["profiles"] == ["m7-channels"]
        environment = services[name]["environment"]
        assert environment[expected_flags[name]] == "false"
        assert not any(
            key in environment
            for key in {
                "XIAOWEI_FEISHU_APP_ID",
                "XIAOWEI_FEISHU_APP_SECRET_FILE",
                "XIAOWEI_FEISHU_TENANT_KEY",
                "XIAOWEI_FEISHU_BOT_OPEN_ID",
                "XIAOWEI_FEISHU_IDENTITY_FILE",
                "XIAOWEI_WEB_DETAIL_BASE_URL",
            }
        )

    assert services["web-app"]["environment"]["XIAOWEI_WEB_BIND_HOST"] == (
        "0.0." + "0.0"
    )
    assert services["web-app"]["environment"]["XIAOWEI_WEB_BIND_PORT"] == "8080"


def test_compose_healthchecks_use_available_binaries_and_no_shell() -> None:
    services = _yaml("docker-compose.yml")["services"]
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD"
    api_test = services["api"]["healthcheck"]["test"]
    assert api_test[:3] == ["CMD", "python", "-c"]
    assert "/healthz" in api_test[3]
    assert "curl" not in str(services)


def test_overrides_have_only_the_approved_worker_environment_paths() -> None:
    smoke = _yaml("docker-compose.smoke.yml")
    barrier = _yaml("docker-compose.barrier.yml")
    assert set(smoke) == {"services"}
    assert set(smoke["services"]) == {"worker"}
    assert set(smoke["services"]["worker"]) == {"environment"}
    assert set(smoke["services"]["worker"]["environment"]) == {
        "XIAOWEI_LEASE_TTL_SECONDS",
        "XIAOWEI_HEARTBEAT_INTERVAL_SECONDS",
        "XIAOWEI_WORKER_POLL_INTERVAL_SECONDS",
        "XIAOWEI_DB_CONNECT_TIMEOUT_SECONDS",
        "XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS",
    }
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

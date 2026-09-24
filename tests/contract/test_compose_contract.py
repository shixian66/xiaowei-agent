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
    "XIAOWEI_FEISHU_TENANT_KEY",
    "XIAOWEI_FEISHU_BOT_OPEN_ID",
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


class _ComposeLoader(yaml.SafeLoader):
    """Compose 的 ``!override`` / ``!reset`` 是**渲染期指令**，静态读取时按普通序列对待。

    没有这个 loader，``yaml.safe_load`` 一遇到未注册标签就抛 ``ConstructorError``，
    于是 ``docker-compose.lan.yml`` 一落地，本文件里所有 compose 契约断言立刻全红。
    """


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(
        _tag, lambda loader, node: loader.construct_sequence(node, deep=True)
    )


def _yaml(name: str) -> dict[str, Any]:
    value = yaml.load(
        (_ROOT / name).read_text(encoding="utf-8"),
        Loader=_ComposeLoader,  # noqa: S506 -- 派生自 SafeLoader，只多认两个标签
    )
    assert isinstance(value, dict)
    return value


_CONFIG_TARGET = "/run/xiaowei-config"
# W4a：每个进程只挂自己消费的域；``web-app`` 是唯一写入方，三域可写。
_CONFIG_DOMAINS_BY_SERVICE = {
    "worker": ("ai", "resources"),
    "feishu-listener": ("feishu",),
    "channel-worker": ("feishu",),
}
_CONFIG_CONSUMERS = set(_CONFIG_DOMAINS_BY_SERVICE)


def _config_mount(domain: str, *, read_only: bool) -> dict[str, Any]:
    mount: dict[str, Any] = {
        "type": "bind",
        "source": f"./.config/{domain}",
        "target": f"{_CONFIG_TARGET}/{domain}",
        "bind": {"create_host_path": False},
    }
    if read_only:
        mount["read_only"] = True
    return mount


_COMPOSE_FILES = frozenset(
    {
        "docker-compose.yml",
        "docker-compose.barrier.yml",
        "docker-compose.lan.yml",
        "docker-compose.m6b-test.yml",
        "docker-compose.model.yml",
        "docker-compose.smoke.yml",
    }
)


def test_every_compose_file_is_registered_and_parses() -> None:
    """仓库里的每一份 Compose 文件都必须在闭集里，并且**本文件读得动**。

    两件事一起钉住。多出来一份未登记的 override 就是一次没人审过的部署面变更；
    而"读得动"是 ``_ComposeLoader`` 的承重点——``!override`` 是渲染期指令，
    ``yaml.safe_load`` 遇到它直接抛 ``ConstructorError``，于是本文件里所有
    compose 契约断言会因为一个与它们无关的原因全红。
    """
    assert {path.name for path in _ROOT.glob("docker-compose*.yml")} == _COMPOSE_FILES
    for name in sorted(_COMPOSE_FILES):
        assert isinstance(_yaml(name), dict), name


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
    # Provider 凭据不再是 Docker secret：override 只剩"这个部署装配了 Gemini"。
    assert override == {
        "services": {"worker": {"environment": {"XIAOWEI_GEMINI_ENABLED": "true"}}}
    }
    assert "secrets" not in override


def _rendered(*files: str, environment: dict[str, str] | None = None) -> dict[str, Any]:
    """用真实 compose CLI 渲染；没有 CLI 时 skip（本文件既有做法）。"""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable; compose-smoke remains the no-skip gate")
    command = _resolve_compose_command(docker=docker, runner=_default_runner)
    arguments: list[str] = [*command]
    for name in files:
        arguments.extend(("-f", str(_ROOT / name)))
    arguments.extend(("config", "--format", "json"))
    result = subprocess.run(  # noqa: S603 -- binary 由 shutil.which 解析
        arguments,
        cwd=_ROOT,
        env=environment if environment is not None else dict(os.environ),
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_rendered_model_config_keeps_key_out_of_environments_and_non_worker_mounts() -> None:
    environment = dict(os.environ)
    environment.pop("GEMINI_API_KEY", None)
    environment["GEMINI_API_KEY_FILE"] = str(
        _ROOT / ".secrets" / "must-not-override-gemini-path"
    )
    rendered = _rendered(
        "docker-compose.yml", "docker-compose.model.yml", environment=environment
    )
    # Gemini 不再有 secret 可以被渲染进来；这一条是"已经拆掉"的正面证据。
    assert "gemini_api_key" not in rendered.get("secrets", {})
    services = rendered["services"]
    assert services["worker"]["environment"]["XIAOWEI_GEMINI_ENABLED"] == "true"
    assert {item["source"] for item in services["worker"]["secrets"]} == {
        "postgres_password"
    }
    for name, service in services.items():
        if name != "worker":
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


def test_rendered_lan_override_replaces_rather_than_appends_the_port() -> None:
    """渲染结果**只有一条**端口映射。

    这条只能靠渲染输出判定：静态读 override 文件永远只看得到它自己写的那一条，
    看不出基础文件的 loopback 映射有没有被换掉。追加语义会渲染出两条，基础的
    "只发 loopback" 就成了一句空话。
    """
    rendered = _rendered("docker-compose.yml", "docker-compose.lan.yml")
    ports = rendered["services"]["web-app"]["ports"]
    assert len(ports) == 1
    assert ports[0]["host_ip"] == "0.0." + "0.0"
    assert ports[0]["published"] == "8080"
    assert not any(port.get("host_ip") == "127.0.0.1" for port in ports)


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


def test_readme_startup_step_names_every_published_host_port() -> None:
    """README 的启动步骤必须点名基础文件发布的每一个宿主端口。

    上面那条已经把 compose 侧钉死了，但**只钉一侧**：README 用手写句子重述同一组
    端口，没有任何东西检查两边是否一致，于是它可以长期停在"只发布 8080"上——实际
    api 还在 8000 上监听。对着这份文档做部署验收的人，验收记录里就会少一个监听面。

    这条断言把两侧接起来：端口以 compose 为准，README 必须逐个提到。
    """
    services = _yaml("docker-compose.yml")["services"]
    published = {
        port.split(":")[1]
        for service in services.values()
        for port in service.get("ports", ())
    }
    assert published == {"8000", "8080"}

    step = _readme_startup_step()
    missing = [port for port in sorted(published) if f"127.0.0.1:{port}" not in step]
    assert not missing, f"README 启动步骤未提到已发布端口：{missing}"


def _readme_startup_step() -> str:
    """取 README 首启流程里"启动"那一步的文本。

    只取这一步而不是整份 README：全文搜索会被架构图、目录树里出现的端口号满足，
    那样这条断言就永远为真。
    """
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("5. 启动。")
    return text[start : text.index("\n6. ", start)]


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
    # Provider 凭据走 ``.config/<域>/config.json``，不再是 Docker secret；
    # 这里只剩部署前就存在的基础设施凭据。
    assert compose["secrets"] == {
        "postgres_password": {"file": "./.secrets/postgres_password"}
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
    for name in _APP_SERVICES | {"postgres"}:
        assert services[name]["secrets"] == ["postgres_password"]
    assert not (_ROOT / ".secrets/postgres_password").exists()
    assert "feishu_app_secret" not in (_ROOT / "docker-compose.yml").read_text(
        encoding="utf-8"
    )


def test_no_long_running_service_mounts_or_names_the_legacy_identity_file() -> None:
    """W5：旧静态身份文件只是一次性迁移命令的输入。

    身份只查 PostgreSQL 目录；任何 Compose 文件里的任何服务都不得挂载旧文档，也不得
    带 ``XIAOWEI_FEISHU_IDENTITY_FILE``。一次性迁移由 runbook 用 ``compose run -v ...:ro``
    临时挂载，命令退出后没有容器继续持有它。
    """
    assert not (_ROOT / "docker-compose.feishu.yml").exists()
    for name in sorted(_COMPOSE_FILES):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "feishu-identities.json" not in text, name
        assert "XIAOWEI_FEISHU_IDENTITY_FILE" not in text, name
        for service in (_yaml(name).get("services") or {}).values():
            for volume in service.get("volumes", []):
                target = volume.get("target") if isinstance(volume, dict) else volume
                assert "identit" not in str(target), (name, target)
    assert not (_ROOT / ".secrets/feishu-identities.json").exists()


def test_the_config_directory_is_writable_only_for_the_web_app() -> None:
    """配置目录的读写属性就是"谁能改这台机器的 Provider 凭据"。

    ``api`` 完全不挂：它既不消费 Provider，也不该在一次读接口的进程里出现明文
    凭据；三个消费进程只读；只有写配置的 ``web-app`` 可写。
    """
    services = _yaml("docker-compose.yml")["services"]
    for name, domains in _CONFIG_DOMAINS_BY_SERVICE.items():
        assert services[name]["volumes"] == [
            _config_mount(domain, read_only=True) for domain in domains
        ]
    assert services["web-app"]["volumes"] == [
        _config_mount(domain, read_only=False) for domain in ("ai", "feishu", "resources")
    ]
    for name in (_APP_SERVICES | {"postgres"}) - _CONFIG_CONSUMERS - {"web-app"}:
        assert all(
            not str(volume.get("target", "")).startswith(_CONFIG_TARGET)
            for volume in services[name].get("volumes", [])
            if isinstance(volume, dict)
        )


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
    """飞书两个进程仍是 profile 后的可选插件；``web-app`` 不是。

    Web 是本里程碑的主入口，留在 profile 里等于普通 ``up -d`` 永远拉不起它——
    runbook 第 5 步会静默什么都不做，而运维要到打不开页面时才发现。
    """
    compose = _yaml("docker-compose.yml")
    services = compose["services"]
    expected_flags = {
        "feishu-listener": {"XIAOWEI_FEISHU_LISTENER_ENABLED": "false"},
        "channel-worker": {"XIAOWEI_CHANNEL_WORKER_ENABLED": "false"},
    }
    for name in _CHANNEL_SERVICES - {"web-app"}:
        assert services[name]["profiles"] == ["m7-channels"]
        environment = services[name]["environment"]
        assert expected_flags[name].items() <= environment.items()
        assert not (_FEISHU_LIVE_ENVIRONMENT & environment.keys())
    assert "profiles" not in services["web-app"]

    assert not (_CHANNEL_ONLY_ENVIRONMENT & compose["x-app-environment"].keys())
    for name in _NON_CHANNEL_APP_SERVICES | {"postgres"}:
        assert not (_CHANNEL_ONLY_ENVIRONMENT & services[name]["environment"].keys())

    assert services["web-app"]["environment"]["XIAOWEI_WEB_BIND_HOST"] == (
        "0.0." + "0.0"
    )
    assert services["web-app"]["environment"]["XIAOWEI_WEB_BIND_PORT"] == "8080"


def test_web_switches_are_interpolated_and_still_default_to_closed() -> None:
    """写字面量时 ``.env`` 改不动它们——Compose 的 ``environment:`` 优先级更高。

    默认值必须仍是关闭：这条改的是"能不能被覆盖"，不是"默认开不开"。
    """
    environment = _yaml("docker-compose.yml")["services"]["web-app"]["environment"]
    expected = {
        "XIAOWEI_WEB_APP_ENABLED": "false",
        "XIAOWEI_FEISHU_OAUTH_ENABLED": "false",
        "XIAOWEI_GEMINI_REAL_TEST_ENABLED": "false",
        "XIAOWEI_FEISHU_REAL_TEST_ENABLED": "false",
        "XIAOWEI_WEB_MODE": "https",
        "XIAOWEI_WEB_PUBLIC_ORIGIN": "",
    }
    for key, default in expected.items():
        assert environment[key] == f"${{{key}:-{default}}}", key


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

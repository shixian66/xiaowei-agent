"""RI5 的部署面边界：发布地址、配置目录读写、镜像身份与预检。

这组用例守的是**部署形状**，不是代码行为。它们之所以值得单独成文件：Compose 的
默认语义（ports 合并、environment 字面量优先、profile 门）每一条都会安静地把一个
本该成立的承诺变成空话，而那种失败只有在真机上才看得见。
"""

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.compose_smoke import _default_runner, _resolve_compose_command

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_TARGET = "/run/xiaowei-config"
_CONFIG_CONSUMERS = ("worker", "feishu-listener", "channel-worker")
_DOMAINS = ("ai", "feishu", "resources")
_ALL_SERVICES = {
    "postgres",
    "migrate",
    "api",
    "worker",
    "feishu-listener",
    "channel-worker",
    "web-app",
}


class _ComposeLoader(yaml.SafeLoader):
    """``!override`` / ``!reset`` 是渲染期指令，静态读取时按普通序列对待。"""


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


def _volumes(service: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in service.get("volumes", []) if isinstance(item, dict)]


# --------------------------------------------------------------------------
# 发布地址
# --------------------------------------------------------------------------


def test_base_compose_publishes_only_loopback() -> None:
    services = _yaml("docker-compose.yml")["services"]
    assert services["web-app"]["ports"] == ["127.0.0.1:8080:8080"]
    for service in services.values():
        for port in service.get("ports", []):
            assert "0.0." + "0.0" not in str(port)


def test_lan_override_only_changes_the_web_port() -> None:
    """LAN override 只碰一个键。

    它是在改密之后才叠加的东西；顺手带上别的改动会让"只放开端口"这句话不再成立，
    而运维在那一步没有理由再读一遍整份文件。
    """
    override = _yaml("docker-compose.lan.yml")
    assert set(override) == {"services"}
    assert set(override["services"]) == {"web-app"}
    assert set(override["services"]["web-app"]) == {"ports"}
    assert override["services"]["web-app"]["ports"] == ["0.0." + "0.0:8080:8080"]


def test_existing_compose_yaml_reader_survives_the_override_tag() -> None:
    """反例：``yaml.safe_load`` 读 LAN override **必须**抛 ConstructorError。

    这条是在证明上面那个 loader 真的承重——而不是一个可有可无的装饰。它一旦被
    某次"清理"删掉，既有的 compose 契约测试会全红，而红的原因与真正的改动无关。
    """
    text = (_ROOT / "docker-compose.lan.yml").read_text(encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load(text)
    assert isinstance(yaml.load(text, Loader=_ComposeLoader), dict)  # noqa: S506


def test_lan_override_replaces_rather_than_appends_the_port() -> None:
    """渲染结果只有一条映射。静态断言看不出这件事，只能问 compose CLI。"""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable; compose-smoke remains the no-skip gate")
    command = _resolve_compose_command(docker=docker, runner=_default_runner)
    result = subprocess.run(  # noqa: S603 -- binary 由 shutil.which 解析
        [
            *command,
            "-f",
            str(_ROOT / "docker-compose.yml"),
            "-f",
            str(_ROOT / "docker-compose.lan.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=_ROOT,
        env=dict(os.environ),
        check=True,
        capture_output=True,
        text=True,
    )
    ports = json.loads(result.stdout)["services"]["web-app"]["ports"]
    assert len(ports) == 1
    assert not any(port.get("host_ip") == "127.0.0.1" for port in ports)


# --------------------------------------------------------------------------
# 装配门与开关
# --------------------------------------------------------------------------


def test_web_app_is_not_gated_behind_a_profile() -> None:
    services = _yaml("docker-compose.yml")["services"]
    assert "profiles" not in services["web-app"]
    for name in ("feishu-listener", "channel-worker"):
        assert services[name]["profiles"] == ["m7-channels"]


def test_web_switches_are_interpolated_not_hardcoded() -> None:
    environment = _yaml("docker-compose.yml")["services"]["web-app"]["environment"]
    switches = {
        "XIAOWEI_WEB_APP_ENABLED",
        "XIAOWEI_FEISHU_OAUTH_ENABLED",
        "XIAOWEI_GEMINI_REAL_TEST_ENABLED",
        "XIAOWEI_FEISHU_REAL_TEST_ENABLED",
    }
    for key in switches:
        # 形如 ${KEY:-false}：既能被 .env 覆盖，默认仍然是关闭。
        assert environment[key] == f"${{{key}:-false}}", key
    assert environment["XIAOWEI_WEB_MODE"] == "${XIAOWEI_WEB_MODE:-https}"


def test_no_compose_file_requires_the_feishu_identity_file() -> None:
    """干净 checkout（没有 ``.secrets/``）必须仍然渲染得出来；W5 起旧身份文件
    不再挂给任何长期服务，失效的飞书 override 也已删除。"""
    for path in sorted(_ROOT.glob("docker-compose*.yml")):
        assert "feishu-identities.json" not in path.read_text(encoding="utf-8"), path.name
    assert not (_ROOT / ".secrets" / "feishu-identities.json").exists()
    assert not (_ROOT / "docker-compose.feishu.yml").exists()


def test_the_old_provider_secrets_are_gone() -> None:
    for name in ("docker-compose.yml", "docker-compose.model.yml"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "gemini_api_" + "key" not in text
        assert "feishu_app_" + "secret" not in text


# --------------------------------------------------------------------------
# 配置目录
# --------------------------------------------------------------------------


def test_api_does_not_mount_any_config_domain() -> None:
    """读接口进程不该在内存里、也不该在文件系统上碰到明文 Provider 凭据。"""
    services = _yaml("docker-compose.yml")["services"]
    for name in ("api", "migrate", "postgres"):
        assert all(
            not str(volume.get("target", "")).startswith(_CONFIG_TARGET)
            for volume in _volumes(services[name])
        ), name


def test_web_mounts_every_domain_read_write_and_nothing_else() -> None:
    volumes = _volumes(_yaml("docker-compose.yml")["services"]["web-app"])
    config = {
        item["target"]: item
        for item in volumes
        if str(item["target"]).startswith(_CONFIG_TARGET)
    }
    assert set(config) == {f"{_CONFIG_TARGET}/{domain}" for domain in _DOMAINS}
    # 只读会让"保存"在点下去之后才失败，而那时管理员已经改过密码、填过凭据。
    assert all("read_only" not in item for item in config.values())


def test_consumers_mount_only_their_own_domains_read_only() -> None:
    services = _yaml("docker-compose.yml")["services"]
    expected = {
        "worker": {"ai", "resources"},
        "feishu-listener": {"feishu"},
        "channel-worker": {"feishu"},
    }
    for name in _CONFIG_CONSUMERS:
        config = [
            item
            for item in _volumes(services[name])
            if str(item["target"]).startswith(_CONFIG_TARGET)
        ]
        assert {item["target"].rsplit("/", 1)[-1] for item in config} == expected[name]
        assert all(item["read_only"] is True for item in config), name


def test_container_target_paths_are_derived_from_the_code_constants() -> None:
    """挂载点与代码里的默认路径必须是同一组常量派生出来的；父目录从不作为目标。"""
    from xiaowei_agent.interfaces.integration_config_file import (
        CONFIG_ROOT,
        DEFAULT_AI_CONFIG_PATH,
        DEFAULT_FEISHU_CONFIG_PATH,
        DEFAULT_RESOURCES_CONFIG_PATH,
    )

    assert CONFIG_ROOT == _CONFIG_TARGET
    defaults = {
        os.path.dirname(path)
        for path in (
            DEFAULT_AI_CONFIG_PATH,
            DEFAULT_FEISHU_CONFIG_PATH,
            DEFAULT_RESOURCES_CONFIG_PATH,
        )
    }
    services = _yaml("docker-compose.yml")["services"]
    targets = {
        volume["target"]
        for service in services.values()
        for volume in _volumes(service)
        if str(volume.get("source", "")).startswith("./.config")
    }
    assert targets == defaults
    assert _CONFIG_TARGET not in targets


def test_the_config_directory_is_ignored_by_git_and_docker() -> None:
    """`.config` 里是明文凭据：既不能进仓库，也不能进镜像构建上下文。"""
    assert ".config/" in (_ROOT / ".gitignore").read_text(encoding="utf-8").split("\n")
    assert ".config" in (_ROOT / ".dockerignore").read_text(
        encoding="utf-8"
    ).splitlines()


# --------------------------------------------------------------------------
# 镜像身份与既有加固
# --------------------------------------------------------------------------


def test_dockerfile_pins_the_numeric_uid_and_gid() -> None:
    """宿主机上的 ``.config`` 属主要跟容器用户对得上，号码就不能是分配来的。"""
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--gid 10001" in dockerfile
    assert "--uid 10001" in dockerfile
    assert "groupadd --system xiaowei &&" not in dockerfile


def test_read_only_rootfs_and_dropped_caps_are_unchanged() -> None:
    """这一轮改的是挂载与端口，不该顺手放宽任何既有加固。"""
    services = _yaml("docker-compose.yml")["services"]
    for name in _ALL_SERVICES - {"postgres"}:
        assert services[name]["read_only"] is True, name
        assert services[name]["cap_drop"] == ["ALL"], name
        assert services[name]["security_opt"] == ["no-new-privileges:true"], name
    assert services["postgres"]["security_opt"] == ["no-new-privileges:true"]


# --------------------------------------------------------------------------
# 预检
# --------------------------------------------------------------------------


def test_preflight_module_lives_inside_the_packaged_source() -> None:
    """``scripts/`` 不进镜像；预检放在那里会在容器里 ``ModuleNotFoundError``。"""
    assert (
        _ROOT / "src" / "xiaowei_agent" / "interfaces" / "config_preflight.py"
    ).is_file()
    assert not (_ROOT / "scripts" / "config_preflight.py").exists()
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY scripts" not in dockerfile


def _config_root(tmp_path: Path) -> Path:
    root = tmp_path / ".config"
    root.mkdir()
    for domain in _DOMAINS:
        (root / domain).mkdir()
    return root


def test_preflight_probes_are_never_a_real_config_file() -> None:
    """反例：哨兵写成 ``config.json`` 会污染干净部署的起点（首次保存从 2 开始）。"""
    from xiaowei_agent.interfaces.config_preflight import probe_file_name
    from xiaowei_agent.interfaces.integration_config_file import (
        CONFIG_FILE_NAME,
        LEGACY_CONFIG_FILE_NAME,
    )

    names = {probe_file_name(domain) for domain in _DOMAINS}
    assert len(names) == len(_DOMAINS)  # 逐目录不同哨兵
    assert not names & {CONFIG_FILE_NAME, LEGACY_CONFIG_FILE_NAME}


def test_preflight_leaves_a_clean_root_untouched(tmp_path: Path) -> None:
    from xiaowei_agent.interfaces.config_preflight import run_preflight

    root = _config_root(tmp_path)
    assert run_preflight(str(root)) == "preflight: ok"
    # 三个域目录一个文件都不剩，"尚未配置"这个起点原封不动。
    assert sorted(path.name for path in root.iterdir()) == sorted(_DOMAINS)
    assert all(list((root / domain).iterdir()) == [] for domain in _DOMAINS)


def test_preflight_does_not_disturb_existing_domain_configurations(tmp_path: Path) -> None:
    """反例：重复预检不得动一份正在被各进程使用的配置。"""
    from xiaowei_agent.contracts import AiConfig, GeminiIntegration
    from xiaowei_agent.interfaces.config_preflight import run_preflight
    from xiaowei_agent.interfaces.integration_config_file import write_ai_config

    root = _config_root(tmp_path)
    existing = root / "ai" / "config.json"
    write_ai_config(
        str(existing),
        AiConfig(
            generation=7,
            gemini=GeminiIntegration(enabled=True, api_key="preflight-" + "fixture-key"),
        ),
    )
    before = existing.read_bytes(), existing.stat().st_ino, existing.stat().st_mtime_ns

    assert run_preflight(str(root)) == "preflight: ok"

    after = existing.read_bytes(), existing.stat().st_ino, existing.stat().st_mtime_ns
    assert after == before
    assert [path.name for path in (root / "ai").iterdir()] == ["config.json"]


@pytest.mark.parametrize("missing", _DOMAINS)
def test_preflight_requires_every_domain_directory(tmp_path: Path, missing: str) -> None:
    from xiaowei_agent.interfaces.config_preflight import PreflightFailure, run_preflight

    root = _config_root(tmp_path)
    (root / missing).rmdir()
    assert run_preflight(str(root)) == PreflightFailure.DIRECTORY_MISSING.value
    (root / missing).symlink_to(tmp_path)
    assert run_preflight(str(root)) == PreflightFailure.DIRECTORY_MISSING.value


def test_preflight_never_creates_a_real_config_file(tmp_path: Path) -> None:
    from xiaowei_agent.interfaces.config_preflight import run_preflight

    root = _config_root(tmp_path)
    assert run_preflight(str(root)) == "preflight: ok"
    assert not any(root.rglob("config.json"))


def test_a_failed_probe_is_still_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei_agent.interfaces import config_preflight
    from xiaowei_agent.interfaces.integration_config_file import (
        PreflightProbeUnreadableError,
    )

    root = _config_root(tmp_path)

    def _write_then_fail(path: str) -> None:
        Path(path).write_text("{}", encoding="utf-8")
        raise PreflightProbeUnreadableError

    monkeypatch.setattr(config_preflight, "write_preflight_probe", _write_then_fail)
    assert (
        config_preflight.run_preflight(str(root))
        == config_preflight.PreflightFailure.PROBE_NOT_READABLE.value
    )
    assert not any(path.is_file() for path in root.rglob("*"))


def test_preflight_reports_a_closed_code_and_never_file_contents() -> None:
    """输出会被贴进工单；它只能是闭集里的一行。"""
    from xiaowei_agent.interfaces.config_preflight import PreflightFailure, run_preflight

    lines = {"preflight: ok", *(member.value for member in PreflightFailure)}
    assert run_preflight("/definitely/not/a/directory") in lines
    assert all(line.startswith("preflight: ") for line in lines)

    source = (
        _ROOT / "src" / "xiaowei_agent" / "interfaces" / "config_preflight.py"
    ).read_text(encoding="utf-8")
    printed = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    # 只有一处 print，参数是一个已经被闭集收敛过的变量。
    assert len(printed) == 1
    assert isinstance(printed[0].args[0], ast.Name)


def test_the_page_shows_the_same_fixed_gemini_constants_as_the_adapter() -> None:
    """前端那三个只读常量与 adapter 锁定的值必须一字不差。

    页面是静态资源，拿不到 Python 常量，因此只能抄一份——抄本身不是问题，**抄了
    之后没人比对**才是。这条用例就是那个比对。
    """
    from xiaowei_agent.interfaces.gemini_model import (
        GEMINI_API_VERSION,
        GEMINI_MODEL,
        GEMINI_PROVIDER_ORIGIN,
    )

    script = (
        _ROOT / "src" / "xiaowei_agent" / "interfaces" / "web_static" / "admin.js"
    ).read_text(encoding="utf-8")
    assert f'model: "{GEMINI_MODEL}"' in script
    assert f'apiVersion: "{GEMINI_API_VERSION}"' in script
    assert f'origin: "{GEMINI_PROVIDER_ORIGIN}"' in script

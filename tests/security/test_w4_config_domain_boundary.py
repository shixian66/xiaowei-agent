"""W4a 三域配置的安全边界：精确挂载矩阵、域隔离、legacy fail-closed 与窄 API。

**挂载必须逐格相等，而不是"有挂载"。** 只断言"worker 挂了 ai"的测试，在有人顺手
把 ``.config`` 父目录也挂进去时依然全绿——而那一刻 worker 就能读到飞书 Secret。
"""

import inspect
import json
import os
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from xiaowei_agent.application.integration_state import (
    SERVICE_CHANNEL_WORKER,
    SERVICE_FEISHU_LISTENER,
    SERVICE_WEB,
    SERVICE_WORKER,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AiConfig,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
)
from xiaowei_agent.interfaces import integration_config_file, provider_consumption
from xiaowei_agent.interfaces.config_preflight import PreflightFailure, run_preflight
from xiaowei_agent.interfaces.integration_config_file import (
    write_ai_config,
    write_feishu_config,
)
from xiaowei_agent.interfaces.integration_config_migrate import (
    MigrationResult,
    migrate_legacy_integration_config,
)

pytestmark = pytest.mark.security

_ROOT: Final = Path(__file__).resolve().parents[2]
_FAKE_KEY: Final = "AIza" + "-w4-boundary-key"
_FAKE_SECRET: Final = "app" + "-w4-boundary-secret"

# (宿主真源, 容器路径, 是否只读)。与计划第 3 节矩阵逐格对应。
_AI: Final = ("./.config/ai", "/run/xiaowei-config/ai")
_FEISHU: Final = ("./.config/feishu", "/run/xiaowei-config/feishu")
_RESOURCES: Final = ("./.config/resources", "/run/xiaowei-config/resources")
_EXPECTED_MOUNTS: Final[dict[str, set[tuple[str, str, bool]]]] = {
    "web-app": {(*_AI, False), (*_FEISHU, False), (*_RESOURCES, False)},
    "worker": {(*_AI, True), (*_RESOURCES, True)},
    "feishu-listener": {(*_FEISHU, True)},
    "channel-worker": {(*_FEISHU, True)},
    "api": set(),
    "migrate": set(),
    "postgres": set(),
}


class _ComposeLoader(yaml.SafeLoader):
    """``!override`` / ``!reset`` 是 Compose 渲染期指令，静态读取时按普通值对待。"""


def _construct_tagged(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    """``!override`` 作用于序列，``!reset`` 还可以作用于 ``null``（W5 release 清除 ``build``）。"""
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return None


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _construct_tagged)


def _compose_services() -> dict[str, Any]:
    document = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    services = document["services"]
    assert isinstance(services, dict)
    return services


def _config_mounts(service: dict[str, Any]) -> set[tuple[str, str, bool]]:
    """所有**指向或来自**配置目录的挂载；短语法一并收下，免得藏过去。"""
    mounts: set[tuple[str, str, bool]] = set()
    for item in service.get("volumes", []):
        if isinstance(item, str):
            source, _, rest = item.partition(":")
            target, _, mode = rest.partition(":")
            entry = (source, target, mode == "ro")
        else:
            entry = (
                str(item.get("source", "")),
                str(item.get("target", "")),
                bool(item.get("read_only", False)),
            )
        if ".config" in entry[0] or entry[1].startswith("/run/xiaowei-config"):
            mounts.add(entry)
    return mounts


def test_compose_mounts_equal_the_three_domain_matrix_cell_by_cell() -> None:
    services = _compose_services()
    assert set(services) == set(_EXPECTED_MOUNTS)
    actual = {name: _config_mounts(service) for name, service in services.items()}
    assert actual == _EXPECTED_MOUNTS


def test_no_service_mounts_the_parent_config_directory() -> None:
    for name, service in _compose_services().items():
        for source, target, _ in _config_mounts(service):
            assert source.rstrip("/") not in {"./.config", ".config"}, name
            assert target.rstrip("/") != "/run/xiaowei-config", name


def test_no_override_file_reintroduces_a_parent_or_foreign_domain_mount() -> None:
    """override 与基础文件按 target 合并：任何一个 override 挂回父目录或别的域，
    叠加后的进程就又能读到兄弟域——只查基础文件等于没查。"""
    for path in sorted(_ROOT.glob("docker-compose*.yml")):
        document = (
            yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=_ComposeLoader,  # noqa: S506 -- 派生自 SafeLoader，只多认两个标签
            )
            or {}
        )
        for name, service in (document.get("services") or {}).items():
            for source, target, read_only in _config_mounts(service or {}):
                assert source.rstrip("/") not in {"./.config", ".config"}, (path.name, name)
                assert target.rstrip("/") != "/run/xiaowei-config", (path.name, name)
                assert (source, target, read_only) in _EXPECTED_MOUNTS[name], (path.name, name)


def test_every_config_mount_refuses_to_create_the_host_path() -> None:
    """宿主目录缺失时必须失败，而不是由 Docker 以 root 身份悄悄建一个空目录。"""
    for name, service in _compose_services().items():
        for item in service.get("volumes", []):
            if isinstance(item, dict) and ".config" in str(item.get("source", "")):
                assert item.get("type") == "bind", name
                assert item.get("bind", {}).get("create_host_path") is False, name


# --------------------------------------------------------------------------
# 每个进程只读、只签自己的域
# --------------------------------------------------------------------------


def _write_domains(tmp_path: Path) -> tuple[str, str]:
    ai_path = tmp_path / "ai" / "config.json"
    feishu_path = tmp_path / "feishu" / "config.json"
    ai_path.parent.mkdir()
    feishu_path.parent.mkdir()
    write_ai_config(
        str(ai_path),
        AiConfig(generation=3, gemini=GeminiIntegration(enabled=True, api_key=_FAKE_KEY)),
    )
    write_feishu_config(
        str(feishu_path),
        FeishuConfig(
            generation=8,
            feishu=FeishuIntegration(enabled=True, app_id="cli_w4", app_secret=_FAKE_SECRET),
        ),
    )
    return str(ai_path), str(feishu_path)


def _all_enabled() -> Settings:
    return Settings(
        environment_id="dev",
        gemini_enabled=True,
        feishu_listener_enabled=True,
        channel_worker_enabled=True,
        feishu_tenant_key="tenant-key",
        feishu_bot_open_id="ou_bot",
        web_public_origin="https://ops.example.test",
    )


@pytest.mark.parametrize(
    ("service_name", "own_domain", "forbidden_reader"),
    [
        (SERVICE_WORKER, "ai", "read_feishu_config"),
        (SERVICE_FEISHU_LISTENER, "feishu", "read_ai_config"),
        (SERVICE_CHANNEL_WORKER, "feishu", "read_ai_config"),
    ],
)
def test_a_consumer_reads_and_signs_only_its_own_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_name: str,
    own_domain: str,
    forbidden_reader: str,
) -> None:
    ai_path, feishu_path = _write_domains(tmp_path)

    def _forbidden(*_: object, **__: object) -> object:
        raise AssertionError(f"{service_name} must not read another domain")

    monkeypatch.setattr(provider_consumption, forbidden_reader, _forbidden)
    credentials, receipts = provider_consumption.load_provider_credentials(
        settings=_all_enabled(),
        service_name=service_name,
        ai_path=ai_path,
        feishu_path=feishu_path,
    )
    assert set(receipts) == {(service_name, own_domain)}
    if own_domain == "ai":
        assert credentials.gemini_api_key == _FAKE_KEY
        assert credentials.feishu_app_secret is None
    else:
        assert credentials.gemini_api_key is None
        assert credentials.feishu_app_secret == _FAKE_SECRET


def test_web_signs_feishu_only_when_oauth_is_enabled_and_never_reads_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai_path, feishu_path = _write_domains(tmp_path)

    def _forbidden(*_: object, **__: object) -> object:
        raise AssertionError("web must not consume the AI domain")

    monkeypatch.setattr(provider_consumption, "read_ai_config", _forbidden)
    oauth = Settings(
        environment_id="dev",
        gemini_enabled=True,
        web_app_enabled=True,
        web_public_origin="https://xiaowei.example.test",
        feishu_oauth_enabled=True,
    )
    _, receipts = provider_consumption.load_provider_credentials(
        settings=oauth, service_name=SERVICE_WEB, ai_path=ai_path, feishu_path=feishu_path
    )
    assert set(receipts) == {(SERVICE_WEB, "feishu")}

    no_oauth = Settings(
        environment_id="dev",
        web_app_enabled=True,
        web_public_origin="https://xiaowei.example.test",
    )
    credentials, receipts = provider_consumption.load_provider_credentials(
        settings=no_oauth, service_name=SERVICE_WEB, ai_path=ai_path, feishu_path=feishu_path
    )
    assert receipts == {}
    assert credentials.feishu_app_secret is None


def test_a_process_cannot_sign_for_a_sibling(tmp_path: Path) -> None:
    """一个 worker 启动不能替 listener 签飞书回执，反之亦然。"""
    ai_path, feishu_path = _write_domains(tmp_path)
    for service_name in (SERVICE_WORKER, SERVICE_FEISHU_LISTENER, SERVICE_CHANNEL_WORKER):
        _, receipts = provider_consumption.load_provider_credentials(
            settings=_all_enabled(),
            service_name=service_name,
            ai_path=ai_path,
            feishu_path=feishu_path,
        )
        assert {name for name, _ in receipts} == {service_name}


# --------------------------------------------------------------------------
# legacy / mixed：只有迁移器与预检能看见旧文件，且一律 fail-closed
# --------------------------------------------------------------------------


def _legacy_document(generation: int = 5) -> dict[str, object]:
    return {
        "generation": generation,
        "gemini": {"enabled": True, "api_key": _FAKE_KEY},
        "feishu": {"enabled": False},
    }


def _config_root(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    root.mkdir(mode=0o700)
    for domain in ("ai", "feishu", "resources"):
        (root / domain).mkdir(mode=0o700)
    return root


def test_preflight_reports_migration_required_while_the_legacy_file_exists(
    tmp_path: Path,
) -> None:
    root = _config_root(tmp_path)
    legacy = root / "integrations.json"
    legacy.write_text(json.dumps(_legacy_document()), encoding="utf-8")
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    assert run_preflight(str(root)) == PreflightFailure.MIGRATION_REQUIRED.value
    after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    assert after == before


def test_mixed_legacy_and_conflicting_split_state_is_migration_required(
    tmp_path: Path,
) -> None:
    root = _config_root(tmp_path)
    legacy = root / "integrations.json"
    legacy.write_text(json.dumps(_legacy_document(generation=5)), encoding="utf-8")
    write_ai_config(
        str(root / "ai" / "config.json"),
        AiConfig(generation=6, gemini=GeminiIntegration()),
    )
    before_ai = (root / "ai" / "config.json").read_bytes()
    assert (
        migrate_legacy_integration_config(config_root=str(root))
        is MigrationResult.MIGRATION_REQUIRED
    )
    assert legacy.exists()
    assert (root / "ai" / "config.json").read_bytes() == before_ai
    assert not (root / "feishu" / "config.json").exists()


def test_no_service_entry_point_calls_the_migrator() -> None:
    """迁移只能由运维显式执行：任何服务启动路径都不得 import 它。"""
    src = _ROOT / "src" / "xiaowei_agent"
    importers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "integration_config_migrate" in path.read_text(encoding="utf-8")
        and path.name != "integration_config_migrate.py"
    )
    assert importers == []


# --------------------------------------------------------------------------
# 文件边界只有固定类型的窄入口
# --------------------------------------------------------------------------

_PUBLIC_FILE_API: Final = {
    "read_ai_config": ("path",),
    "write_ai_config": ("path", "config"),
    "read_feishu_config": ("path",),
    "write_feishu_config": ("path", "config"),
    # W4b：resources 域同样只有一对固定类型入口。
    "read_resources_config": ("path",),
    "write_resources_config": ("path", "config"),
    "read_legacy_integration_config": ("path",),
    "write_preflight_probe": ("path",),
}


def test_the_file_boundary_exposes_only_fixed_type_entry_points() -> None:
    public = {
        name: tuple(inspect.signature(value).parameters)
        for name, value in vars(integration_config_file).items()
        if inspect.isfunction(value)
        and not name.startswith("_")
        and value.__module__ == integration_config_file.__name__
    }
    assert public == _PUBLIC_FILE_API
    assert "write_integration_config" not in vars(integration_config_file)
    assert "read_integration_config" not in vars(integration_config_file)


def test_no_public_file_function_accepts_a_schema_domain_or_serializer() -> None:
    for name in _PUBLIC_FILE_API:
        parameters = set(inspect.signature(getattr(integration_config_file, name)).parameters)
        assert not parameters & {"schema", "model", "domain", "serializer", "cls"}, name


def test_legacy_file_is_not_visible_through_any_default_runtime_path() -> None:
    for path in (
        integration_config_file.DEFAULT_AI_CONFIG_PATH,
        integration_config_file.DEFAULT_FEISHU_CONFIG_PATH,
        integration_config_file.DEFAULT_RESOURCES_CONFIG_PATH,
    ):
        assert os.path.basename(path) == "config.json"
        assert "integrations" not in path

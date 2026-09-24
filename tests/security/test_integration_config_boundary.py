"""三域配置文件的读写边界：符号链接、非正规文件、超长、schema 与原子写失败一律故障。

AI、飞书与 resources（W4b）三个域共用同一对私有 primitive，因此每条边界用例都对三个域
各跑一遍：只测一个域的套件无法发现另一个域的窄入口绕过了 primitive。
"""

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from xiaowei_agent.contracts import (
    AiConfig,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
)
from xiaowei_agent.contracts.resource_config import (
    PrometheusResource,
    ResourcesConfig,
    StarRocksResource,
)
from xiaowei_agent.interfaces import integration_config_file
from xiaowei_agent.interfaces.integration_config_file import (
    IntegrationConfigError,
    IntegrationConfigMissingError,
    PreflightProbeUnreadableError,
    read_ai_config,
    read_feishu_config,
    read_legacy_integration_config,
    read_resources_config,
    write_ai_config,
    write_feishu_config,
    write_preflight_probe,
    write_resources_config,
)

pytestmark = pytest.mark.security

_FAKE_KEY: Final = "AIza" + "-boundary-not-real"
_FAKE_SECRET: Final = "app" + "-boundary-not-real"
_FAKE_DB_PASSWORD: Final = "sr" + "-boundary-not-real"
_FAKE_TOKEN: Final = "prom" + "-boundary-not-real"


def _resources(generation: int) -> ResourcesConfig:
    return ResourcesConfig(
        generation=generation,
        resources=(
            StarRocksResource(
                kind="starrocks",
                resource_id="1" * 32,
                environment="prod",
                display_name="sr",
                host="10.0.0.1",
                port=9030,
                database="ods",
                username="reader",
                password=_FAKE_DB_PASSWORD,
                tls_mode="verify_ca",
                enabled=True,
            ),
            PrometheusResource(
                kind="prometheus",
                resource_id="2" * 32,
                environment="prod",
                display_name="prom",
                base_url="https://prom.example.internal/p",
                auth_mode="bearer",
                secret=_FAKE_TOKEN,
                tls_mode="verify_identity",
                enabled=False,
            ),
        ),
    )

Reader = Callable[[str], Any]
Writer = Callable[[str, Any], None]

_DOMAINS: Final[dict[str, tuple[Reader, Writer, Callable[[int], Any], dict[str, object]]]] = {
    "ai": (
        read_ai_config,
        write_ai_config,
        lambda generation: AiConfig(
            generation=generation,
            gemini=GeminiIntegration(enabled=True, api_key=_FAKE_KEY),
        ),
        {"generation": 1, "gemini": {"enabled": False}},
    ),
    "feishu": (
        read_feishu_config,
        write_feishu_config,
        lambda generation: FeishuConfig(
            generation=generation,
            feishu=FeishuIntegration(enabled=True, app_id="cli_b", app_secret=_FAKE_SECRET),
        ),
        {"generation": 1, "feishu": {"enabled": False}},
    ),
    "resources": (
        read_resources_config,
        write_resources_config,
        _resources,
        {"generation": 1, "resources": []},
    ),
}



@pytest.fixture(params=sorted(_DOMAINS))
def domain(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _write_raw(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_roundtrip_preserves_generation_and_secret(tmp_path: Path, domain: str) -> None:
    reader, writer, build, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    writer(str(target), build(3))
    assert reader(str(target)) == build(3)


def test_written_file_is_owner_only_and_leaves_no_temporary(
    tmp_path: Path, domain: str
) -> None:
    _, writer, build, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    writer(str(target), build(1))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [path.name for path in tmp_path.iterdir()] == ["config.json"]


def test_symlink_is_refused(tmp_path: Path, domain: str) -> None:
    reader, _, _, minimal = _DOMAINS[domain]
    real = tmp_path / "real.json"
    _write_raw(real, minimal)
    link = tmp_path / "config.json"
    link.symlink_to(real)
    with pytest.raises(IntegrationConfigError) as caught:
        reader(str(link))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_a_broken_symlink_is_a_failure_not_an_absence(tmp_path: Path, domain: str) -> None:
    """断链是本条的要害：os.path.exists() 会说它不存在，从而把它降级成「尚未配置」。"""
    reader, _, _, _ = _DOMAINS[domain]
    link = tmp_path / "config.json"
    link.symlink_to(tmp_path / "nowhere.json")
    assert os.path.exists(link) is False
    with pytest.raises(IntegrationConfigError) as caught:
        reader(str(link))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_only_a_truly_absent_path_reports_missing(tmp_path: Path, domain: str) -> None:
    reader, _, _, _ = _DOMAINS[domain]
    with pytest.raises(IntegrationConfigMissingError):
        reader(str(tmp_path / "config.json"))
    assert issubclass(IntegrationConfigMissingError, IntegrationConfigError)


def test_non_regular_file_is_refused(tmp_path: Path, domain: str) -> None:
    reader, _, _, _ = _DOMAINS[domain]
    fifo = tmp_path / "config.json"
    os.mkfifo(fifo)
    with pytest.raises(IntegrationConfigError) as caught:
        reader(str(fifo))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_relative_path_is_refused(domain: str) -> None:
    reader, writer, build, _ = _DOMAINS[domain]
    with pytest.raises(IntegrationConfigError):
        reader("config.json")
    with pytest.raises(IntegrationConfigError):
        writer("config.json", build(1))


@pytest.mark.parametrize("payload", [b"", b"[" + b"0," * 40_000 + b"0]", b"{bad json"])
def test_empty_oversize_or_malformed_file_is_refused(
    tmp_path: Path, domain: str, payload: bytes
) -> None:
    reader, _, _, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    target.write_bytes(payload)
    with pytest.raises(IntegrationConfigError) as caught:
        reader(str(target))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_schema_violations_are_refused(tmp_path: Path, domain: str) -> None:
    reader, _, _, minimal = _DOMAINS[domain]
    wrong_section: dict[str, object] = {
        "ai": {"gemini": {"enabled": False, "model": "x"}},
        "feishu": {"feishu": {"enabled": False, "model": "x"}},
        "resources": {"resources": [{"kind": "kafka", "resource_id": "3" * 32}]},
    }[domain]
    for payload in (
        {**minimal, "generation": 0},
        {key: value for key, value in minimal.items() if key != "generation"},
        {**minimal, "extra": 1},
        {**minimal, **wrong_section},
        # 旧组合文档放在新路径上必须失败，不能被当成"只看自己那一节"。
        {"generation": 1, "gemini": {"enabled": False}, "feishu": {"enabled": False}},
    ):
        target = tmp_path / "config.json"
        _write_raw(target, payload)
        with pytest.raises(IntegrationConfigError):
            reader(str(target))


def test_error_text_never_repeats_file_content(tmp_path: Path, domain: str) -> None:
    """被拒绝的文档里带着 secret，拒绝路径不得把它回填进异常。"""
    reader, _, _, _ = _DOMAINS[domain]
    fake = "sentinel" + "-secret-value"
    section = (
        {"gemini": {"enabled": True, "api_key": fake, "model": "x"}}
        if domain == "ai"
        else {"feishu": {"enabled": True, "app_secret": fake, "model": "x"}}
    )
    target = tmp_path / "config.json"
    _write_raw(target, {"generation": 1, **section})
    with pytest.raises(IntegrationConfigError) as excinfo:
        reader(str(target))
    assert fake not in str(excinfo.value)
    assert fake not in repr(excinfo.value)
    assert fake not in str(excinfo.value.__context__)


def test_failed_replace_keeps_the_old_file_and_removes_the_temporary(
    tmp_path: Path, domain: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, writer, build, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    writer(str(target), build(1))
    before = target.read_bytes()

    def _refuse(*_: object, **__: object) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(integration_config_file.os, "replace", _refuse)
    with pytest.raises(IntegrationConfigError) as caught:
        writer(str(target), build(2))
    assert "replace refused" not in str(caught.value)
    monkeypatch.undo()
    assert target.read_bytes() == before
    assert [path.name for path in tmp_path.iterdir()] == ["config.json"]
    assert reader(str(target)).generation == 1


def test_failed_file_fsync_keeps_the_old_file(
    tmp_path: Path, domain: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, writer, build, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    writer(str(target), build(1))
    before = target.read_bytes()

    def _refuse(_: int) -> None:
        raise OSError("fsync refused")

    monkeypatch.setattr(integration_config_file.os, "fsync", _refuse)
    with pytest.raises(IntegrationConfigError):
        writer(str(target), build(2))
    monkeypatch.undo()
    assert target.read_bytes() == before
    assert [path.name for path in tmp_path.iterdir()] == ["config.json"]


def test_failed_directory_fsync_is_reported_not_swallowed(
    tmp_path: Path, domain: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录 fsync 失败时新内容可能已可见但持久性未知：必须报失败，不能返回成功。"""
    _, writer, build, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    writer(str(target), build(1))
    real_fsync = os.fsync
    calls: list[int] = []

    def _second_fails(descriptor: int) -> None:
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError("directory fsync refused")
        real_fsync(descriptor)

    monkeypatch.setattr(integration_config_file.os, "fsync", _second_fails)
    with pytest.raises(IntegrationConfigError):
        writer(str(target), build(2))
    assert len(calls) == 2
    assert [path.name for path in tmp_path.iterdir()] == ["config.json"]


def test_write_into_a_missing_directory_is_a_closed_failure(
    tmp_path: Path, domain: str
) -> None:
    _, writer, build, _ = _DOMAINS[domain]
    with pytest.raises(IntegrationConfigError):
        writer(str(tmp_path / "absent" / "config.json"), build(1))


def test_writer_refuses_the_other_domain_document(tmp_path: Path, domain: str) -> None:
    _, writer, _, _ = _DOMAINS[domain]
    other = {"ai": "feishu", "feishu": "resources", "resources": "ai"}[domain]
    with pytest.raises(IntegrationConfigError):
        writer(str(tmp_path / "config.json"), _DOMAINS[other][2](1))
    assert not (tmp_path / "config.json").exists()


def test_read_secret_file_still_refuses_this_json(tmp_path: Path, domain: str) -> None:
    """单行 secret reader 不得成为这份 JSON 的第二条读取路径。"""
    from xiaowei_agent.interfaces.secret_file import SecretFileError, read_secret_file

    _, writer, build, _ = _DOMAINS[domain]
    target = tmp_path / "config.json"
    writer(str(target), build(1))
    with pytest.raises(SecretFileError):
        read_secret_file(str(target))


# --------------------------------------------------------------------------
# 旧文档读取器与预检哨兵：只各一个窄入口
# --------------------------------------------------------------------------


def test_legacy_reader_is_strict_and_symlink_refusing(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    _write_raw(
        target,
        {
            "generation": 2,
            "gemini": {"enabled": True, "api_key": _FAKE_KEY},
            "feishu": {"enabled": False},
        },
    )
    assert read_legacy_integration_config(str(target)).gemini.secret_value() == _FAKE_KEY
    _write_raw(target, {"generation": 2, "gemini": {"enabled": False}})
    with pytest.raises(IntegrationConfigError):
        read_legacy_integration_config(str(target))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(IntegrationConfigError):
        read_legacy_integration_config(str(link))


def test_preflight_probe_uses_the_atomic_writer_and_is_no_domain_document(
    tmp_path: Path,
) -> None:
    probe = tmp_path / ".preflight-probe.json"
    write_preflight_probe(str(probe))
    assert stat.S_IMODE(probe.stat().st_mode) == 0o600
    # 哨兵不是任何域的合法文档：残留也不会被当成配置。
    for reader in (read_ai_config, read_feishu_config):
        with pytest.raises(IntegrationConfigError) as caught:
            reader(str(probe))
        assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_preflight_probe_reports_an_unreadable_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreadable(_: str) -> object:
        raise IntegrationConfigError("integration config unavailable")

    monkeypatch.setattr(integration_config_file, "_read_document", _unreadable)
    with pytest.raises(PreflightProbeUnreadableError):
        write_preflight_probe(str(tmp_path / ".preflight-probe.json"))


# --------------------------------------------------------------------------
# resources：Secret 落盘但不外泄，大小上限按 100 个资源设计
# --------------------------------------------------------------------------


def test_resources_writer_keeps_every_secret_and_the_reader_restores_them(
    tmp_path: Path,
) -> None:
    """不能用 ``model_dump()`` 落盘：Secret 字段 ``exclude=True``，写回去就清空了凭据。"""
    target = tmp_path / "config.json"
    write_resources_config(str(target), _resources(4))
    document = json.loads(target.read_text(encoding="utf-8"))
    assert [item["resource_id"] for item in document["resources"]] == ["1" * 32, "2" * 32]
    restored = read_resources_config(str(target))
    assert restored == _resources(4)
    starrocks, prometheus = restored.resources
    assert isinstance(starrocks, StarRocksResource) and starrocks.password == _FAKE_DB_PASSWORD
    assert isinstance(prometheus, PrometheusResource) and prometheus.secret == _FAKE_TOKEN


def test_a_full_document_of_one_hundred_resources_with_maximal_secrets_fits(
    tmp_path: Path,
) -> None:
    big = "s" * 4096
    config = ResourcesConfig(
        generation=1,
        resources=tuple(
            StarRocksResource(
                kind="starrocks",
                resource_id=f"{index:032x}",
                environment="prod",
                display_name="d" * 128,
                host="h" * 63 + ".example",
                port=9030,
                database="b" * 128,
                username="u" * 128,
                password=big,
                tls_mode="verify_identity",
                enabled=True,
            )
            for index in range(100)
        ),
    )
    target = tmp_path / "config.json"
    write_resources_config(str(target), config)
    assert read_resources_config(str(target)) == config


def test_a_resources_file_above_the_size_bound_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_bytes(b'{"generation": 1, "resources": [], "pad": "' + b"x" * 1_100_000 + b'"}')
    with pytest.raises(IntegrationConfigError) as caught:
        read_resources_config(str(target))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_resources_errors_never_repeat_a_secret(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    _write_raw(
        target,
        {
            "generation": 1,
            "resources": [
                {
                    "kind": "prometheus",
                    "resource_id": "4" * 32,
                    "environment": "prod",
                    "display_name": "p",
                    "base_url": "http://prom",
                    "auth_mode": "none",
                    "secret": _FAKE_TOKEN,
                    "tls_mode": "disabled",
                    "enabled": True,
                }
            ],
        },
    )
    with pytest.raises(IntegrationConfigError) as caught:
        read_resources_config(str(target))
    assert _FAKE_TOKEN not in str(caught.value)
    assert _FAKE_TOKEN not in repr(caught.value)

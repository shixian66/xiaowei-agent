"""Provider 凭据的运行消费：双层开关、按域读取、断供行为与回执语义。

W4a 起每个进程只读自己挂载的那个域文件：worker 读 AI，listener/channel-worker 读飞书，
Web 只在启用 OAuth 时读飞书。回执键是 ``(service_name, config_domain)``。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from xiaowei_agent.application.integration_state import (
    SERVICE_FEISHU_LISTENER,
    SERVICE_WORKER,
    LoadReceipt,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ConfigDomain
from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials


def _write_ai(tmp_path: Path, *, generation: int = 1, **gemini: Any) -> Path:
    target = tmp_path / "ai" / "config.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps({"generation": generation, "gemini": {"enabled": False, **gemini}}),
        encoding="utf-8",
    )
    return target


def _write_feishu(tmp_path: Path, *, generation: int = 1, **feishu: Any) -> Path:
    target = tmp_path / "feishu" / "config.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps({"generation": generation, "feishu": {"enabled": False, **feishu}}),
        encoding="utf-8",
    )
    return target


def _paths(tmp_path: Path) -> dict[str, str]:
    return {
        "ai_path": str(tmp_path / "ai" / "config.json"),
        "feishu_path": str(tmp_path / "feishu" / "config.json"),
    }


@pytest.fixture
def settings_with_gemini_assembled() -> Settings:
    """worker 进程：`.env` 装配了 Gemini。"""
    return Settings(environment_id="dev", gemini_enabled=True)


@pytest.fixture
def settings_all_disabled() -> Settings:
    return Settings(environment_id="dev")


@pytest.fixture
def settings_with_feishu_listener() -> Settings:
    return Settings(
        environment_id="dev",
        feishu_listener_enabled=True,
        feishu_tenant_key="tenant-key",
        feishu_bot_open_id="ou_bot",
        feishu_identity_file="/run/config/feishu-identities.json",
    )


def test_json_disabled_provider_yields_no_credential(
    tmp_path, settings_with_gemini_assembled
) -> None:
    """.env 装配了，但 JSON 里 enabled=false —— 两层与关系，最终不启用。"""
    _write_ai(tmp_path, enabled=False, api_key="k" * 8)
    creds, _ = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None


def test_json_cannot_enable_a_provider_the_env_did_not_assemble(
    tmp_path, settings_all_disabled
) -> None:
    """JSON 不能反向启动 Compose 中未装配的进程。"""
    _write_ai(tmp_path, enabled=True, api_key="k" * 8)
    creds, receipts = load_provider_credentials(
        settings=settings_all_disabled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None
    assert receipts == {}  # 未启用的服务不写回执，不会造成永久「待应用」


def test_both_layers_true_yields_the_credential(
    tmp_path, settings_with_gemini_assembled
) -> None:
    _write_ai(tmp_path, enabled=True, api_key="k" * 8)
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key == "k" * 8
    assert receipts[("worker", ConfigDomain.AI)].status == "loaded"


def test_absent_file_writes_no_receipt_and_does_not_crash(
    tmp_path, settings_with_gemini_assembled
) -> None:
    """读不出文件就没有可信 generation，不得伪造一个写进回执。"""
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None
    assert receipts == {}


def test_corrupt_file_writes_no_receipt_and_does_not_crash(
    tmp_path, settings_with_gemini_assembled
) -> None:
    bad = tmp_path / "ai" / "config.json"
    bad.parent.mkdir()
    bad.write_text("{not json", encoding="utf-8")
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None
    assert receipts == {}


def test_the_legacy_combined_file_is_never_read(
    tmp_path, settings_with_gemini_assembled
) -> None:
    """运行时不双读、不回落：只有旧文件时等同"尚未配置"，不写回执。"""
    (tmp_path / "integrations.json").write_text(
        json.dumps(
            {
                "generation": 3,
                "gemini": {"enabled": True, "api_key": "k" * 8},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None
    assert receipts == {}


def test_readable_file_with_a_bad_provider_subtree_writes_an_invalid_receipt(
    tmp_path, settings_with_gemini_assembled
) -> None:
    """文件本身可读 -> generation 可信 -> 该域记 invalid。"""
    _write_ai(tmp_path, generation=7, enabled=True)  # 缺 api_key
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None
    assert receipts[("worker", ConfigDomain.AI)] == LoadReceipt(
        generation=7, status="invalid"
    )


def test_a_listener_writes_only_its_own_feishu_receipt(
    tmp_path, settings_with_feishu_listener
) -> None:
    """只为本进程实际启用且需要的域写回执，不替别人记账。"""
    _write_ai(tmp_path, enabled=True, api_key="k" * 8)
    _write_feishu(tmp_path, enabled=True, app_id="cli_x", app_secret="s" * 8)
    creds, receipts = load_provider_credentials(
        settings=settings_with_feishu_listener,
        service_name=SERVICE_FEISHU_LISTENER,
        **_paths(tmp_path),
    )
    assert creds.feishu_app_secret == "s" * 8
    assert creds.gemini_api_key is None  # listener 没装配 Gemini
    assert set(receipts) == {("feishu_listener", ConfigDomain.FEISHU)}


def test_ai_and_feishu_generations_are_signed_independently(
    tmp_path, settings_with_feishu_listener
) -> None:
    _write_ai(tmp_path, generation=11, enabled=True, api_key="k" * 8)
    _write_feishu(tmp_path, generation=2, enabled=True, app_id="cli_x", app_secret="s" * 8)
    _, receipts = load_provider_credentials(
        settings=settings_with_feishu_listener,
        service_name=SERVICE_FEISHU_LISTENER,
        **_paths(tmp_path),
    )
    assert receipts[("feishu_listener", ConfigDomain.FEISHU)].generation == 2


def test_gemini_adapter_no_longer_owns_a_path_constant() -> None:
    """adapter 只接受注入的 key，不得自己去文件系统找。"""
    from xiaowei_agent.interfaces import gemini_model

    assert not hasattr(gemini_model, "GEMINI_SECRET_FILE")


def test_every_feishu_adapter_takes_an_in_memory_secret() -> None:
    """四个 adapter 都不得再收文件路径——否则会把明文 Secret 当路径 open()。"""
    import inspect

    from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter
    from xiaowei_agent.interfaces.feishu_sdk import (
        FeishuSdkInboundTransport,
        FeishuSdkMembershipAdapter,
        FeishuSdkMessageAdapter,
    )

    for adapter in (
        FeishuOAuthAdapter,
        FeishuSdkInboundTransport,
        FeishuSdkMessageAdapter,
        FeishuSdkMembershipAdapter,
    ):
        params = inspect.signature(adapter.__init__).parameters
        assert "app_secret" in params, adapter.__name__
        assert "app_secret_file" not in params, adapter.__name__


def test_credentials_repr_hides_the_secrets() -> None:
    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    gemini = "g" + "-fake-key"
    feishu = "f" + "-fake-secret"
    creds = ProviderCredentials(
        gemini_api_key=gemini, feishu_app_id="cli_x", feishu_app_secret=feishu
    )
    rendered = repr(creds)
    assert gemini not in rendered
    assert feishu not in rendered
    assert "cli_x" in rendered  # app_id 不是 secret，保留便于排障


def test_no_feishu_adapter_reads_the_filesystem_at_construction(monkeypatch) -> None:
    """反例：构造期不得再 open() 任何文件。"""
    import builtins

    opened: list[str] = []
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1]
    )
    from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter

    FeishuOAuthAdapter(app_id="cli_x", app_secret="s" * 8, timeout_seconds=5.0)
    assert opened == []


def test_a_broken_symlink_does_not_produce_an_unconfigured_startup(
    tmp_path, settings_with_gemini_assembled
) -> None:
    """断链不能被当成「尚未配置」照常签回执。"""
    link = tmp_path / "ai" / "config.json"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "nowhere.json")
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled,
        service_name=SERVICE_WORKER,
        **_paths(tmp_path),
    )
    assert creds.gemini_api_key is None
    assert receipts == {}  # 读不出可信 generation，不写回执

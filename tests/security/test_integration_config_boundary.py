"""`integrations.json` 的读写边界：符号链接、非正规文件、超长与 schema 一律故障。"""

import json
import os
import stat
from pathlib import Path

import pytest

from xiaowei_agent.contracts import FeishuIntegration, GeminiIntegration, IntegrationConfig
from xiaowei_agent.interfaces.integration_config_file import (
    IntegrationConfigError,
    IntegrationConfigMissingError,
    read_integration_config,
    write_integration_config,
)

pytestmark = pytest.mark.security


def _write_raw(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_roundtrip_preserves_generation_and_flags(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    config = IntegrationConfig(
        generation=3,
        gemini=GeminiIntegration(enabled=True, api_key="k" * 8),
        feishu=FeishuIntegration(enabled=False),
    )
    write_integration_config(str(target), config)
    loaded = read_integration_config(str(target))
    assert loaded.generation == 3
    assert loaded.gemini.enabled is True
    assert loaded.gemini.secret_value() == "k" * 8


def test_written_file_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    write_integration_config(
        str(target),
        IntegrationConfig(generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()),
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    write_integration_config(
        str(target),
        IntegrationConfig(generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()),
    )
    assert [p.name for p in tmp_path.iterdir()] == ["integrations.json"]


def test_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    _write_raw(real, {"generation": 1, "gemini": {"enabled": False}, "feishu": {"enabled": False}})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(IntegrationConfigError) as caught:
        read_integration_config(str(link))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_a_broken_symlink_is_a_failure_not_an_absence(tmp_path: Path) -> None:
    """断链是本条的要害：os.path.exists() 会说它不存在，从而把它降级成「尚未配置」。"""
    link = tmp_path / "integrations.json"
    link.symlink_to(tmp_path / "nowhere.json")
    assert os.path.exists(link) is False
    assert os.path.lexists(link) is True
    with pytest.raises(IntegrationConfigError) as caught:
        read_integration_config(str(link))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_only_a_truly_absent_path_reports_missing(tmp_path: Path) -> None:
    with pytest.raises(IntegrationConfigMissingError):
        read_integration_config(str(tmp_path / "integrations.json"))


def test_missing_is_a_subclass_so_unaware_callers_still_fail_closed() -> None:
    """反例守护：如果把它改成独立异常，所有只捕获基类的调用方会漏网。"""
    assert issubclass(IntegrationConfigMissingError, IntegrationConfigError)


def test_non_regular_file_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(IntegrationConfigError):
        read_integration_config(str(fifo))


def test_relative_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IntegrationConfigError):
        read_integration_config("integrations.json")


def test_oversize_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    target.write_text("[" + "0," * 200_000 + "0]", encoding="utf-8")
    with pytest.raises(IntegrationConfigError):
        read_integration_config(str(target))


@pytest.mark.parametrize(
    "payload",
    [
        {"generation": 0, "gemini": {"enabled": False}, "feishu": {"enabled": False}},
        {"gemini": {"enabled": False}, "feishu": {"enabled": False}},
        {"generation": 1, "gemini": {"enabled": False}, "feishu": {"enabled": False}, "extra": 1},
        {"generation": 1, "gemini": {"enabled": False, "model": "x"}, "feishu": {"enabled": False}},
    ],
)
def test_schema_violations_are_refused(tmp_path: Path, payload: object) -> None:
    target = tmp_path / "integrations.json"
    _write_raw(target, payload)
    with pytest.raises(IntegrationConfigError):
        read_integration_config(str(target))


def test_error_text_never_repeats_file_content(tmp_path: Path) -> None:
    """被拒绝的文档里带着 secret，拒绝路径不得把它回填进异常。

    payload 必须**同时**非法且携带 secret：合法 payload 根本不进拒绝路径，
    这条就什么也证明不了。这里用多出来的 ``model`` 键触发 ``extra="forbid"``，
    而同一棵子树里放着 ``api_key``——正是 ``hide_input_in_errors`` 要堵的通道。
    """
    fake = "sentinel" + "-secret-value"
    target = tmp_path / "integrations.json"
    _write_raw(
        target,
        {
            "generation": 1,
            "gemini": {"enabled": True, "api_key": fake, "model": "x"},
            "feishu": {"enabled": False},
        },
    )
    with pytest.raises(IntegrationConfigError) as excinfo:
        read_integration_config(str(target))
    assert fake not in str(excinfo.value)
    assert fake not in repr(excinfo.value)
    # `from None` 只设置 __suppress_context__，原始 ValidationError 仍挂在异常上；
    # 它不泄露原文靠的是 Contract 基类的 hide_input_in_errors=True。
    assert fake not in str(excinfo.value.__context__)


def test_read_secret_file_still_refuses_this_json(tmp_path: Path) -> None:
    """旧 reader 不得成为 JSON 的第二条读取路径。"""
    from xiaowei_agent.interfaces.secret_file import SecretFileError, read_secret_file

    target = tmp_path / "integrations.json"
    write_integration_config(
        str(target),
        IntegrationConfig(generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()),
    )
    with pytest.raises(SecretFileError):
        read_secret_file(str(target))

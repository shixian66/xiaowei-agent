"""旧 Provider 凭据真源必须彻底消失，且不得误杀迁移后的正当字段名。"""

import pathlib

import pytest

pytestmark = pytest.mark.security

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_no_source_file_references_the_legacy_provider_secret_paths() -> None:
    """只扫 src/：ADR、迁移说明和本测试自身当然会提到旧路径，那是历史记录。

    注意**不能**把 `feishu_app_id` 整体列入禁词——新的 `ProviderCredentials` 自己就有
    `feishu_app_id` 字段，那是迁移后的正当用法。禁的是**旧真源**：Settings 上的同名字段与
    文件路径读取，不是这个名字本身。
    """
    banned = (
        "/run/secrets/gemini_api_key",
        "app_secret_file",            # 任何仍收文件路径的 adapter 签名
        "settings.feishu_app_id",     # 旧 Settings 字段的读取点
        "GEMINI_SECRET_FILE",
    )
    offenders = [
        f"{path.relative_to(_SRC)}:{n}"
        for path in _SRC.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for token in banned
        if token in line
    ]
    assert offenders == [], f"旧 Provider 凭据真源仍被引用：{offenders}"


def test_the_new_credentials_field_is_not_caught_by_the_ban() -> None:
    """反例：确认上一条不会误杀迁移后的正当字段名。"""
    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    assert "feishu_app_id" in ProviderCredentials.__dataclass_fields__


def test_settings_no_longer_exposes_the_legacy_feishu_fields() -> None:
    from xiaowei_agent.config import _FIELD_TO_ENV, Settings

    assert "feishu_app_id" not in Settings.model_fields
    assert "feishu_app_secret_file" not in Settings.model_fields
    assert "XIAOWEI_FEISHU_APP_ID" not in _FIELD_TO_ENV.values()
    assert "XIAOWEI_FEISHU_APP_SECRET_FILE" not in _FIELD_TO_ENV.values()

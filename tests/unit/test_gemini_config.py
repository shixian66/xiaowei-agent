"""Gemini 只开放一个默认关闭的应用配置。"""

import pytest

from xiaowei_agent.config import _FIELD_TO_ENV, ConfigError, Settings, load_settings


def test_gemini_setting_is_exactly_one_default_off_boolean() -> None:
    assert Settings(environment_id="dev").gemini_enabled is False
    assert _FIELD_TO_ENV["gemini_enabled"] == "XIAOWEI_GEMINI_ENABLED"
    assert {
        name for name in Settings.model_fields if name.startswith("gemini_")
    } == {"gemini_enabled"}


def test_gemini_setting_can_be_explicitly_enabled() -> None:
    assert load_settings(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_GEMINI_ENABLED": "true",
        }
    ).gemini_enabled is True


@pytest.mark.parametrize(
    "name",
    [
        "XIAOWEI_GEMINI_MODEL",
        "XIAOWEI_GEMINI_ENDPOINT",
        "XIAOWEI_GEMINI_PROXY",
        "XIAOWEI_GEMINI_TIMEOUT_SECONDS",
        "XIAOWEI_GEMINI_API_KEY",
        "XIAOWEI_GEMINI_API_KEY_FILE",
        "XIAOWEI_GEMINI_SECRET_PATH",
    ],
)
def test_gemini_provider_profile_cannot_be_configured_from_environment(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", name: "forbidden"})

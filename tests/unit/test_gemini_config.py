"""Gemini 只开放一个默认关闭的应用配置。"""

import pytest

from xiaowei_agent.config import _FIELD_TO_ENV, ConfigError, Settings, load_settings


def test_gemini_settings_are_exactly_two_default_off_booleans() -> None:
    """Gemini 在配置面上只有开关，没有凭据、端点或模型名。

    RI5 追加 ``gemini_real_test_enabled``（控制面连通性测试的独立授权），
    因此闭集从一个变成两个；这条断言守的是"只有布尔开关"，不是"只有一个"。
    """
    settings = Settings(environment_id="dev")
    assert settings.gemini_enabled is False
    assert settings.gemini_real_test_enabled is False
    assert _FIELD_TO_ENV["gemini_enabled"] == "XIAOWEI_GEMINI_ENABLED"
    assert _FIELD_TO_ENV["gemini_real_test_enabled"] == "XIAOWEI_GEMINI_REAL_TEST_ENABLED"
    gemini_fields = {
        name for name in Settings.model_fields if name.startswith("gemini_")
    }
    assert gemini_fields == {"gemini_enabled", "gemini_real_test_enabled"}
    assert all(
        Settings.model_fields[name].annotation is bool for name in gemini_fields
    )


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

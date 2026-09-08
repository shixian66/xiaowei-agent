"""M7 飞书 listener 配置闭集与默认关闭语义。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.config import ConfigError, Settings, load_settings

_COMPLETE_PROFILE: dict[str, object] = {
    "feishu_listener_enabled": True,
    "feishu_app_id": "cli_test_app",
    "feishu_app_secret_file": "/run/secrets/feishu_app_secret",
    "feishu_tenant_key": "tenant-test",
    "feishu_bot_open_id": "bot-open-id",
    "feishu_identity_file": "/run/config/feishu-identities.json",
}


def test_feishu_listener_is_disabled_without_any_live_profile_by_default() -> None:
    settings = Settings(environment_id="dev")

    assert settings.feishu_listener_enabled is False
    assert settings.feishu_app_id is None
    assert settings.feishu_app_secret_file is None
    assert settings.feishu_tenant_key is None
    assert settings.feishu_bot_open_id is None
    assert settings.feishu_identity_file is None


@pytest.mark.parametrize(
    "field", sorted(set(_COMPLETE_PROFILE) - {"feishu_listener_enabled"})
)
def test_enabled_listener_requires_every_profile_field(field: str) -> None:
    profile = dict(_COMPLETE_PROFILE)
    del profile[field]

    with pytest.raises(ValidationError, match="complete Feishu listener profile"):
        Settings(environment_id="dev", **profile)


def test_live_profile_without_explicit_enable_flag_is_rejected_as_disabled() -> None:
    profile = dict(_COMPLETE_PROFILE)
    del profile["feishu_listener_enabled"]

    with pytest.raises(ValidationError, match="disabled Feishu listener"):
        Settings(environment_id="dev", **profile)


@pytest.mark.parametrize("field", sorted(set(_COMPLETE_PROFILE) - {"feishu_listener_enabled"}))
def test_disabled_listener_rejects_stray_live_profile_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="disabled Feishu listener"):
        Settings(environment_id="dev", **{field: _COMPLETE_PROFILE[field]})


def test_complete_listener_profile_loads_from_environment() -> None:
    settings = load_settings(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_FEISHU_LISTENER_ENABLED": "true",
            "XIAOWEI_FEISHU_APP_ID": "cli_test_app",
            "XIAOWEI_FEISHU_APP_SECRET_FILE": "/run/secrets/feishu_app_secret",
            "XIAOWEI_FEISHU_TENANT_KEY": "tenant-test",
            "XIAOWEI_FEISHU_BOT_OPEN_ID": "bot-open-id",
            "XIAOWEI_FEISHU_IDENTITY_FILE": "/run/config/feishu-identities.json",
        }
    )

    assert settings.feishu_listener_enabled is True
    assert settings.feishu_app_id == "cli_test_app"
    assert settings.feishu_identity_file == "/run/config/feishu-identities.json"


def test_blank_optional_feishu_values_in_example_style_are_treated_as_absent() -> None:
    settings = load_settings(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_FEISHU_LISTENER_ENABLED": "false",
            "XIAOWEI_FEISHU_APP_ID": "",
            "XIAOWEI_FEISHU_APP_SECRET_FILE": "",
            "XIAOWEI_FEISHU_TENANT_KEY": "",
            "XIAOWEI_FEISHU_BOT_OPEN_ID": "",
            "XIAOWEI_FEISHU_IDENTITY_FILE": "",
        }
    )

    assert settings.feishu_listener_enabled is False
    assert settings.feishu_app_id is None


@pytest.mark.parametrize(
    "field",
    ["feishu_app_secret_file", "feishu_identity_file"],
)
def test_feishu_file_references_must_be_absolute(field: str) -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        Settings(environment_id="dev", **(_COMPLETE_PROFILE | {field: "relative/file"}))


def test_invalid_feishu_profile_does_not_expose_values() -> None:
    shaped = "cli_" + "SHOULD-NOT-LEAK"
    env = {
        "XIAOWEI_ENVIRONMENT_ID": "dev",
        "XIAOWEI_FEISHU_LISTENER_ENABLED": "true",
        "XIAOWEI_FEISHU_APP_ID": shaped,
    }

    with pytest.raises(ConfigError) as caught:
        load_settings(env)

    assert shaped not in str(caught.value)

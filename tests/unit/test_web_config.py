"""Web app 的默认关闭、完整配置与 TTL 边界。"""

import pytest

from xiaowei_agent.config import ConfigError, Settings, load_settings


def _profile(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment_id": "dev",
        "web_app_enabled": True,
        "feishu_oauth_enabled": True,
        "feishu_app_id": "cli_test_app",
        "feishu_app_secret_file": "/run/secrets/feishu_app_secret",
        "feishu_identity_file": "/run/config/feishu-identities.json",
        "web_detail_base_url": "https://ops.example.test",
    }
    return values | updates


def test_web_app_is_default_closed_with_short_bounded_lifetimes() -> None:
    settings = Settings(environment_id="dev")

    assert settings.web_app_enabled is False
    assert settings.feishu_oauth_enabled is False
    assert settings.web_bind_host == "127.0.0.1"
    assert settings.web_bind_port == 8080
    assert settings.web_oauth_state_ttl_seconds == 300
    assert settings.web_session_ttl_seconds == 3600


@pytest.mark.parametrize(
    "field",
    [
        "feishu_app_id",
        "feishu_app_secret_file",
        "feishu_identity_file",
        "web_detail_base_url",
    ],
)
def test_enabled_web_app_requires_the_complete_auth_profile(field: str) -> None:
    profile = _profile()
    del profile[field]

    with pytest.raises(ValueError):
        Settings(**profile)


def test_web_app_profile_loads_from_the_explicit_environment_only() -> None:
    settings = load_settings(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
            "XIAOWEI_WEB_BIND_HOST": "127.0.0.1",
            "XIAOWEI_WEB_BIND_PORT": "8081",
            "XIAOWEI_WEB_OAUTH_STATE_TTL_SECONDS": "120",
            "XIAOWEI_WEB_SESSION_TTL_SECONDS": "7200",
            "XIAOWEI_FEISHU_APP_ID": "cli_test_app",
            "XIAOWEI_FEISHU_APP_SECRET_FILE": "/run/secrets/feishu_app_secret",
            "XIAOWEI_FEISHU_IDENTITY_FILE": "/run/config/feishu-identities.json",
            "XIAOWEI_WEB_DETAIL_BASE_URL": "https://OPS.example.test/",
        }
    )

    assert settings.web_app_enabled is True
    assert settings.feishu_oauth_enabled is True
    assert settings.web_bind_port == 8081
    assert settings.web_oauth_state_ttl_seconds == 120
    assert settings.web_session_ttl_seconds == 7200
    assert settings.web_detail_base_url == "https://ops.example.test"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_oauth_state_ttl_seconds", 0),
        ("web_oauth_state_ttl_seconds", 601),
        ("web_session_ttl_seconds", 0),
        ("web_session_ttl_seconds", 86_401),
        ("web_bind_port", 0),
        ("web_bind_port", 65_536),
    ],
)
def test_web_app_timing_and_bind_values_are_bounded(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        Settings(**_profile(**{field: value}))


@pytest.mark.parametrize(
    ("web_enabled", "oauth_enabled"),
    [(True, False), (False, True)],
    ids=["web-without-oauth", "oauth-without-web"],
)
def test_web_and_oauth_activation_flags_must_move_together(
    web_enabled: bool, oauth_enabled: bool
) -> None:
    profile = _profile(
        web_app_enabled=web_enabled,
        feishu_oauth_enabled=oauth_enabled,
    )
    if not web_enabled:
        for field in (
            "feishu_app_id",
            "feishu_app_secret_file",
            "feishu_identity_file",
            "web_detail_base_url",
        ):
            profile.pop(field)

    with pytest.raises(ValueError, match="OAuth"):
        Settings(**profile)


@pytest.mark.parametrize(
    "origin",
    ["https://127.0.0.1:8443", "https://[::1]:8443"],
    ids=["ipv4", "ipv6"],
)
def test_oauth_web_origin_requires_a_hostname(origin: str) -> None:
    with pytest.raises(ValueError, match="hostname"):
        Settings(**_profile(web_detail_base_url=origin))


def test_disabled_web_app_cannot_carry_a_web_only_identity_profile() -> None:
    with pytest.raises(ValueError):
        Settings(
            environment_id="dev",
            feishu_app_id="cli_test_app",
            feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
            feishu_identity_file="/run/config/feishu-identities.json",
            web_detail_base_url="https://ops.example.test",
        )


def test_invalid_web_profile_does_not_echo_configuration_values() -> None:
    sensitive = "invalid-origin.example.test/path?code=" + "provider-code"
    with pytest.raises(ConfigError) as caught:
        load_settings(
            {
                "XIAOWEI_ENVIRONMENT_ID": "dev",
                "XIAOWEI_WEB_APP_ENABLED": "true",
                "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
                "XIAOWEI_FEISHU_APP_ID": "cli_test_app",
                "XIAOWEI_FEISHU_APP_SECRET_FILE": "/run/secrets/feishu_app_secret",
                "XIAOWEI_FEISHU_IDENTITY_FILE": "/run/config/feishu-identities.json",
                "XIAOWEI_WEB_DETAIL_BASE_URL": sensitive,
            }
        )

    assert sensitive not in str(caught.value)

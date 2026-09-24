"""Web app 的默认关闭、完整配置与 TTL 边界。"""

import pytest

from xiaowei_agent.config import (
    ConfigError,
    Settings,
    canonical_web_public_origin,
    load_settings,
)
from xiaowei_agent.contracts import WebMode


def _profile(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment_id": "dev",
        "web_app_enabled": True,
        "feishu_oauth_enabled": True,
        "web_public_origin": "https://ops.example.test",
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


def test_enabled_web_app_requires_the_complete_auth_profile() -> None:
    profile = _profile()
    del profile["web_public_origin"]

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
            "XIAOWEI_WEB_PUBLIC_ORIGIN": "https://OPS.example.test/",
        }
    )

    assert settings.web_app_enabled is True
    assert settings.feishu_oauth_enabled is True
    assert settings.web_bind_port == 8081
    assert settings.web_oauth_state_ttl_seconds == 120
    assert settings.web_session_ttl_seconds == 7200
    assert settings.web_public_origin == "https://ops.example.test"


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


def test_feishu_oauth_still_requires_the_web_app() -> None:
    """解耦是单向的：Web 可以没有飞书，飞书 OAuth 不能没有 Web。

    OAuth 登录本身就是 Web 的一条路由，没有 Web 进程就没有 callback 落点。
    """
    profile = _profile(web_app_enabled=False, feishu_oauth_enabled=True)
    profile.pop("web_public_origin")

    with pytest.raises(ValueError, match="OAuth"):
        Settings(**profile)


def test_feishu_oauth_with_a_full_profile_still_needs_the_web_app() -> None:
    """反例：补齐全部飞书字段也不行——web_app_enabled=False 时 origin 本身不被允许。"""
    with pytest.raises(ValueError):
        Settings(**_profile(web_app_enabled=False, feishu_oauth_enabled=True))


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8443",
        "https://[::1]:8443",
        "https://127.1",
        "https://127.000.000.001",
        "https://2130706433",
        "https://0x7f000001",
        "https://0177.0.0.1",
        "https://0x7f.1",
    ],
    ids=[
        "ipv4",
        "ipv6",
        "short-ipv4",
        "zero-padded-ipv4",
        "decimal-ipv4",
        "hex-ipv4",
        "octal-component-ipv4",
        "hex-component-ipv4",
    ],
)
def test_oauth_web_origin_requires_a_hostname(origin: str) -> None:
    with pytest.raises(ValueError, match="hostname"):
        Settings(**_profile(web_public_origin=origin))


@pytest.mark.parametrize(
    "origin",
    [
        "https://ops.example.test:0",
        "https://ops.example.test?",
        "https://ops.example.test#",
        "https://ops.\nexample.test",
        "https://ops.\texample.test",
        "https://ops.\rexample.test",
    ],
    ids=[
        "port-zero",
        "empty-query",
        "empty-fragment",
        "line-feed",
        "tab",
        "carriage-return",
    ],
)
def test_web_origin_rejects_invalid_authority_syntax(origin: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        Settings(**_profile(web_public_origin=origin))


def test_disabled_web_app_cannot_carry_a_web_only_identity_profile() -> None:
    with pytest.raises(ValueError):
        Settings(
            environment_id="dev",
            web_public_origin="https://ops.example.test",
        )


def test_invalid_web_profile_does_not_echo_configuration_values() -> None:
    sensitive = "invalid-origin.example.test/path?code=" + "provider-code"
    with pytest.raises(ConfigError) as caught:
        load_settings(
            {
                "XIAOWEI_ENVIRONMENT_ID": "dev",
                "XIAOWEI_WEB_APP_ENABLED": "true",
                "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
                "XIAOWEI_WEB_PUBLIC_ORIGIN": sensitive,
            }
        )

    assert sensitive not in str(caught.value)


def _env(**updates: str) -> dict[str, str]:
    """最小可用环境；只带必填键，其余由 Settings 默认值补齐。"""
    values: dict[str, str] = {"XIAOWEI_ENVIRONMENT_ID": "dev"}
    return values | updates


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8080",
        "http://192.168.1.20:8080",
        "http://10.0.0.5:8080",
        "http://172.16.0.9:8080",
    ],
)
def test_lan_http_accepts_loopback_and_rfc1918_with_explicit_port(value: str) -> None:
    assert canonical_web_public_origin(value, mode=WebMode.LAN_HTTP) == value


@pytest.mark.parametrize(
    "value",
    [
        "http://8.8.8.8:8080",          # 公网 IP
        "http://192.168.1.20",          # 缺显式端口
        "https://192.168.1.20:8080",    # 协议与模式错配
        "http://example.com:8080",      # lan_http 不接受主机名
        "http://192.168.1.20:8080/app", # 带路径
        "http://user@192.168.1.20:8080",
    ],
)
def test_lan_http_rejects_everything_else(value: str) -> None:
    with pytest.raises(ValueError):
        canonical_web_public_origin(value, mode=WebMode.LAN_HTTP)


@pytest.mark.parametrize(
    "value",
    ["http://192.168.1.20:8080", "https://192.168.1.20:8080", "https://127.0.0.1:8443"],
)
def test_https_mode_still_rejects_http_and_ip_literals(value: str) -> None:
    with pytest.raises(ValueError):
        canonical_web_public_origin(value, mode=WebMode.HTTPS)


def test_https_mode_accepts_the_existing_sso_hostname_shape() -> None:
    assert (
        canonical_web_public_origin("https://sso.example.com", mode=WebMode.HTTPS)
        == "https://sso.example.com"
    )


def test_web_app_no_longer_requires_feishu_oauth() -> None:
    settings = load_settings(
        _env(
            XIAOWEI_WEB_APP_ENABLED="true",
            XIAOWEI_FEISHU_OAUTH_ENABLED="false",
            XIAOWEI_WEB_MODE="lan_http",
            XIAOWEI_WEB_PUBLIC_ORIGIN="http://127.0.0.1:8080",
        )
    )
    assert settings.web_app_enabled is True
    assert settings.feishu_oauth_enabled is False


def test_real_test_switches_default_to_false() -> None:
    settings = load_settings(_env())
    assert settings.gemini_real_test_enabled is False
    assert settings.feishu_real_test_enabled is False


def test_web_app_still_requires_a_public_origin() -> None:
    with pytest.raises(ConfigError):
        load_settings(_env(XIAOWEI_WEB_APP_ENABLED="true", XIAOWEI_WEB_MODE="lan_http"))


def test_disabled_web_and_worker_must_not_carry_a_public_origin() -> None:
    with pytest.raises(ConfigError):
        load_settings(_env(XIAOWEI_WEB_PUBLIC_ORIGIN="https://sso.example.com"))

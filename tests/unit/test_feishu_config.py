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

_WORKER_PROFILE: dict[str, object] = {
    "channel_worker_enabled": True,
    "feishu_app_id": "cli_test_app",
    "feishu_app_secret_file": "/run/secrets/feishu_app_secret",
    "web_detail_base_url": "https://ops.example.test",
}


def test_feishu_listener_is_disabled_without_any_live_profile_by_default() -> None:
    settings = Settings(environment_id="dev")

    assert settings.feishu_listener_enabled is False
    assert settings.channel_worker_enabled is False
    assert settings.feishu_app_id is None
    assert settings.feishu_app_secret_file is None
    assert settings.feishu_tenant_key is None
    assert settings.feishu_bot_open_id is None
    assert settings.feishu_identity_file is None
    assert settings.web_detail_base_url is None
    assert settings.feishu_api_timeout_seconds == 5.0
    assert settings.projection_claim_ttl_seconds == 15
    assert settings.projection_provider_max_attempts == 3
    assert settings.projection_tenant_concurrency == 2


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


@pytest.mark.parametrize(
    "field", sorted(set(_WORKER_PROFILE) - {"channel_worker_enabled"})
)
def test_enabled_channel_worker_requires_every_live_profile_field(field: str) -> None:
    profile = dict(_WORKER_PROFILE)
    del profile[field]

    with pytest.raises(ValidationError, match="complete channel worker profile"):
        Settings(environment_id="dev", **profile)


def test_worker_only_profile_does_not_require_listener_identity_configuration() -> None:
    settings = Settings(environment_id="dev", **_WORKER_PROFILE)

    assert settings.channel_worker_enabled is True
    assert settings.feishu_listener_enabled is False
    assert settings.feishu_tenant_key is None
    assert settings.feishu_bot_open_id is None
    assert settings.feishu_identity_file is None
    assert settings.web_detail_base_url == "https://ops.example.test"


def test_listener_and_worker_can_share_one_explicit_app_credential_reference() -> None:
    settings = Settings(
        environment_id="dev",
        **(_COMPLETE_PROFILE | _WORKER_PROFILE),
    )

    assert settings.feishu_listener_enabled is True
    assert settings.channel_worker_enabled is True


def test_complete_channel_worker_profile_loads_from_environment() -> None:
    settings = load_settings(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_CHANNEL_WORKER_ENABLED": "true",
            "XIAOWEI_FEISHU_APP_ID": "cli_test_app",
            "XIAOWEI_FEISHU_APP_SECRET_FILE": "/run/secrets/feishu_app_secret",
            "XIAOWEI_WEB_DETAIL_BASE_URL": "https://ops.example.test/",
        }
    )

    assert settings.channel_worker_enabled is True
    assert settings.web_detail_base_url == "https://ops.example.test"


@pytest.mark.parametrize(
    "url",
    [
        "http://ops.example.test",
        "https://user@ops.example.test",
        "https://ops.example.test/app",
        "https://ops.example.test?tenant=other",
        "https://ops.example.test#fragment",
        "https://ops.exa mple.test",
        "https://.example.test",
        "https://example..test",
        "https://bad$.example.test",
    ],
    ids=[
        "http",
        "userinfo",
        "path",
        "query",
        "fragment",
        "hostname-space",
        "empty-leading-label",
        "empty-middle-label",
        "invalid-host-character",
    ],
)
def test_web_detail_base_url_is_a_trusted_https_origin_only(url: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS origin"):
        Settings(
            environment_id="dev",
            **(_WORKER_PROFILE | {"web_detail_base_url": url}),
        )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ops-internal", "https://ops-internal"),
        ("https://127.0.0.1:8443", "https://127.0.0.1:8443"),
        ("https://[::1]:8443", "https://[::1]:8443"),
        ("https://täst.de", "https://xn--tst-qla.de"),
        ("https://ops.example.test:443", "https://ops.example.test"),
        (
            "https://ops.example.test⁄evil",
            "https://ops.example.xn--testevil-h03d",
        ),
        ("https://OPS.EXAMPLE.TEST:8443/", "https://ops.example.test:8443"),
    ],
    ids=[
        "single-label",
        "ipv4",
        "ipv6",
        "idna",
        "default-port",
        "confusable-slash",
        "case",
    ],
)
def test_valid_https_origin_host_forms_are_stored_canonically(
    url: str, expected: str
) -> None:
    settings = Settings(
        environment_id="dev",
        **(_WORKER_PROFILE | {"web_detail_base_url": url}),
    )

    assert settings.web_detail_base_url == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"feishu_api_timeout_seconds": 13, "projection_claim_ttl_seconds": 15},
        {
            "projection_provider_backoff_base_seconds": 2,
            "projection_retry_after_cap_seconds": 1,
        },
        {
            "projection_task_poll_base_seconds": 11,
            "projection_task_poll_cap_seconds": 10,
        },
        {"channel_worker_poll_interval_seconds": 4},
    ],
    ids=["claim-margin", "provider-backoff", "task-poll", "worker-poll"],
)
def test_projection_timing_relationships_fail_closed(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="projection"):
        Settings(environment_id="dev", **(_WORKER_PROFILE | overrides))


def test_projection_tenant_concurrency_cannot_exceed_two() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment_id="dev",
            **(_WORKER_PROFILE | {"projection_tenant_concurrency": 3}),
        )


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

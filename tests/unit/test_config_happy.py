"""配置的正常路径。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from xiaowei_agent.config import _FIELD_TO_ENV, DEFAULT_TENANT_ID, Settings, load_settings


def _test_readonly_env(password_file: Path) -> dict[str, str]:
    return {
        "XIAOWEI_ENVIRONMENT_ID": "test",
        "XIAOWEI_ACTOR": "m6b-operator",
        "XIAOWEI_STARROCKS_ADAPTER_MODE": "test_readonly",
        "XIAOWEI_STARROCKS_HOST": "starrocks.test.invalid",
        "XIAOWEI_STARROCKS_PORT": "9030",
        "XIAOWEI_STARROCKS_DATABASE": "audit_db",
        "XIAOWEI_STARROCKS_USER": "audit_reader",
        "XIAOWEI_STARROCKS_PASSWORD_FILE": str(password_file),
        "XIAOWEI_STARROCKS_TLS_MODE": "verify_identity",
        "XIAOWEI_STARROCKS_CA_FILE": "/approved/ca.pem",
        "XIAOWEI_STARROCKS_SERVER_NAME": "starrocks.test.invalid",
        "XIAOWEI_STARROCKS_RESOURCE_ID": "approved-test-cluster",
        "XIAOWEI_STARROCKS_EXPECTED_VERSION_SHA256": "d" * 64,
        "XIAOWEI_STARROCKS_EXPECTED_GRANTS_SHA256": "a" * 64,
        "XIAOWEI_STARROCKS_EXPECTED_DDL_SHA256": "b" * 64,
        "XIAOWEI_STARROCKS_EXPECTED_IDENTITY_SHA256": "c" * 64,
        "XIAOWEI_STARROCKS_EXPECTED_METADATA_SOURCE_REF": "approval-ref:m6b-1",
        "XIAOWEI_STARROCKS_PHYSICAL_IDENTITY_REF": "identity-ref:test-cluster",
        "XIAOWEI_STARROCKS_AUTHORIZED_ACTOR": "m6b-operator",
        "XIAOWEI_STARROCKS_ACTIVE_FROM": "2026-09-07T09:00:00+08:00",
        "XIAOWEI_STARROCKS_ACTIVE_UNTIL": "2026-09-07T11:00:00+08:00",
        "XIAOWEI_STARROCKS_CONNECT_TIMEOUT_SECONDS": "5",
        "XIAOWEI_STARROCKS_READ_TIMEOUT_SECONDS": "25",
        "XIAOWEI_STARROCKS_WRITE_TIMEOUT_SECONDS": "5",
        "XIAOWEI_STARROCKS_QUERY_TIMEOUT_SECONDS": "20",
    }


def test_load_from_explicit_mapping() -> None:
    s = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_LOG_LEVEL": "DEBUG"})
    assert s.environment_id == "dev"
    assert s.log_level == "DEBUG"


def test_log_level_defaults_to_info() -> None:
    assert load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}).log_level == "INFO"


def test_model_configuration_surface_is_only_default_off_flags() -> None:
    settings = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"})
    assert settings.gemini_enabled is False
    assert settings.gemini_real_test_enabled is False
    assert {
        field: environment
        for field, environment in _FIELD_TO_ENV.items()
        if field.startswith("gemini_")
    } == {
        "gemini_enabled": "XIAOWEI_GEMINI_ENABLED",
        "gemini_real_test_enabled": "XIAOWEI_GEMINI_REAL_TEST_ENABLED",
    }


def test_tenant_id_is_a_constant() -> None:
    s = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"})
    assert s.tenant_id == DEFAULT_TENANT_ID == "dev-local"


def test_settings_is_frozen() -> None:
    s = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"})
    with pytest.raises(ValidationError):
        s.environment_id = "other"


def test_m5_database_and_process_defaults_are_explicit() -> None:
    settings = Settings(environment_id="dev")
    assert settings.postgres_host == "postgres"
    assert settings.postgres_port == 5432
    assert settings.postgres_database == "xiaowei"
    assert settings.postgres_user == "xiaowei"
    assert settings.postgres_password_file == "/run/secrets/postgres_" + "password"
    assert settings.db_connect_timeout_seconds == 5.0
    assert settings.db_command_timeout_seconds == 15.0
    assert settings.db_pool_size == 5
    assert settings.db_pool_max_overflow == 0
    assert settings.api_bind_host == "127.0.0.1"
    assert settings.api_bind_port == 8000


def test_starrocks_defaults_to_recording_without_live_configuration() -> None:
    settings = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"})

    assert settings.starrocks_adapter_mode == "recording"
    assert settings.starrocks_host is None
    assert settings.starrocks_password_file is None


def test_complete_starrocks_test_readonly_configuration_loads(tmp_path: Path) -> None:
    password_file = tmp_path / "database-credential"
    password_file.write_text("not-read-by-settings\n", encoding="utf-8")

    settings = load_settings(_test_readonly_env(password_file))

    assert settings.environment_id == "test"
    assert settings.starrocks_adapter_mode == "test_readonly"
    assert settings.starrocks_host == "starrocks.test.invalid"
    assert settings.starrocks_authorized_actor == "m6b-operator"
    assert settings.starrocks_query_timeout_seconds == 20


def test_starrocks_config_revision_is_stable_and_never_reads_password(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "database-credential"
    password_file.write_text("first-credential\n", encoding="utf-8")
    settings = load_settings(_test_readonly_env(password_file))

    first = settings.starrocks_config_revision(
        driver_version="1.2.0",
        normalizer_version="1",
        sql_surface_ref="starrocks.slow-query@1",
    )
    password_file.write_text("rotated-credential\n", encoding="utf-8")
    second = settings.starrocks_config_revision(
        driver_version="1.2.0",
        normalizer_version="1",
        sql_surface_ref="starrocks.slow-query@1",
    )

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("XIAOWEI_STARROCKS_HOST", "starrocks-2.test.invalid"),
        ("XIAOWEI_STARROCKS_RESOURCE_ID", "approved-test-cluster-2"),
        ("XIAOWEI_STARROCKS_TLS_MODE", "verify_ca"),
        ("XIAOWEI_STARROCKS_READ_TIMEOUT_SECONDS", "24"),
        ("XIAOWEI_STARROCKS_EXPECTED_VERSION_SHA256", "e" * 64),
        ("XIAOWEI_STARROCKS_PASSWORD_FILE", "/approved/rotated-credential-ref"),
    ],
)
def test_starrocks_config_revision_covers_connection_policy(
    tmp_path: Path,
    name: str,
    replacement: str,
) -> None:
    password_file = tmp_path / "database-credential"
    password_file.write_text("not-read-by-settings\n", encoding="utf-8")
    base_env = _test_readonly_env(password_file)
    base = load_settings(base_env).starrocks_config_revision(
        driver_version="1.2.0",
        normalizer_version="1",
        sql_surface_ref="starrocks.slow-query@1",
    )
    changed_env = {**base_env, name: replacement}
    if name == "XIAOWEI_STARROCKS_HOST":
        changed_env["XIAOWEI_STARROCKS_SERVER_NAME"] = replacement
    changed = load_settings(changed_env).starrocks_config_revision(
        driver_version="1.2.0",
        normalizer_version="1",
        sql_surface_ref="starrocks.slow-query@1",
    )

    assert changed != base


@pytest.mark.parametrize(
    ("argument", "replacement"),
    [
        ("driver_version", "1.2.1"),
        ("normalizer_version", "2"),
        ("sql_surface_ref", "starrocks.slow-query@2"),
    ],
)
def test_starrocks_config_revision_covers_runtime_implementation(
    tmp_path: Path,
    argument: str,
    replacement: str,
) -> None:
    settings = load_settings(_test_readonly_env(tmp_path / "database-credential"))
    base_args = {
        "driver_version": "1.2.0",
        "normalizer_version": "1",
        "sql_surface_ref": "starrocks.slow-query@1",
    }

    base = settings.starrocks_config_revision(**base_args)
    changed = settings.starrocks_config_revision(**{**base_args, argument: replacement})

    assert changed != base


def test_all_documented_fields_load_from_the_environment() -> None:
    settings = load_settings(
        {
            "XIAOWEI_ENVIRONMENT_ID": "staging",
            "XIAOWEI_POSTGRES_HOST": "db.internal",
            "XIAOWEI_POSTGRES_PORT": "5544",
            "XIAOWEI_POSTGRES_DATABASE": "agentdb",
            "XIAOWEI_POSTGRES_USER": "agentuser",
            "XIAOWEI_POSTGRES_PASSWORD_FILE": "/var/run/example/" + "password-file",
            "XIAOWEI_DB_CONNECT_TIMEOUT_SECONDS": "3",
            "XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS": "12",
            "XIAOWEI_DB_POOL_SIZE": "7",
            "XIAOWEI_DB_POOL_MAX_OVERFLOW": "2",
            "XIAOWEI_API_BIND_HOST": "::1",
            "XIAOWEI_API_BIND_PORT": "9000",
        }
    )
    assert settings.postgres_host == "db.internal"
    assert settings.postgres_port == 5544
    assert settings.postgres_database == "agentdb"
    assert settings.postgres_user == "agentuser"
    assert settings.postgres_password_file == "/var/run/example/" + "password-file"
    assert settings.db_connect_timeout_seconds == 3.0
    assert settings.db_command_timeout_seconds == 12.0
    assert settings.db_pool_size == 7
    assert settings.db_pool_max_overflow == 2
    assert settings.api_bind_host == "::1"
    assert settings.api_bind_port == 9000

"""配置的正常路径。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.config import DEFAULT_TENANT_ID, Settings, load_settings


def test_load_from_explicit_mapping() -> None:
    s = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_LOG_LEVEL": "DEBUG"})
    assert s.environment_id == "dev"
    assert s.log_level == "DEBUG"


def test_log_level_defaults_to_info() -> None:
    assert load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}).log_level == "INFO"


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

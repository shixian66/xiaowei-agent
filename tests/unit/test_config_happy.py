"""配置的正常路径。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.config import DEFAULT_TENANT_ID, load_settings


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

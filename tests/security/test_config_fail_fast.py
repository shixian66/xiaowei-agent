"""配置 fail-fast 与不泄漏取值的承重测试。"""

import os
import subprocess
import sys
import traceback

import pytest

from xiaowei_agent.config import ConfigError, load_settings

pytestmark = pytest.mark.security

_SECRET = "SUPER-" + "SECRET-" + "VALUE-XYZ"


def test_explicit_env_does_not_fall_back_to_host_environ() -> None:
    """传入 env 时绝不回退读宿主环境（pydantic BaseSettings 会回退，故用 BaseModel）。"""
    os.environ["XIAOWEI_ENVIRONMENT_ID"] = "HOST-LEAK"
    with pytest.raises(ConfigError):
        load_settings({})


def test_missing_required_field_fails_fast() -> None:
    with pytest.raises(ConfigError):
        load_settings({})


def test_tenant_id_cannot_be_overridden_by_env() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_TENANT_ID": "attacker"})
    assert "XIAOWEI_TENANT_ID" in str(exc.value)


def test_lowercase_variant_is_rejected_not_silently_used() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({"xiaowei_environment_id": "sneaky"})
    assert "xiaowei_environment_id" in str(exc.value)


def test_mixed_case_duplicate_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings({"XIAOWEI_ENVIRONMENT_ID": "UPPER", "xiaowei_environment_id": "lower"})


@pytest.mark.parametrize("bad", ["", "   ", " prod ", "prod "])
def test_blank_or_padded_environment_id_rejected(bad: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({"XIAOWEI_ENVIRONMENT_ID": bad})


def test_unknown_variable_message_has_name_but_not_value() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_UNKNOWN": _SECRET})
    msg = str(exc.value)
    assert "XIAOWEI_UNKNOWN" in msg
    assert _SECRET not in msg


def test_invalid_value_absent_from_message_and_full_traceback() -> None:
    """仅检查 str(exc) 不够：隐式异常链会把原值留在完整 traceback 中。"""
    try:
        load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_LOG_LEVEL": _SECRET})
    except ConfigError as exc:
        assert _SECRET not in str(exc)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert _SECRET not in tb
        assert exc.__cause__ is None
        # from None 只设 __suppress_context__，不清 __context__；
        # 因此实现必须在 except 块之外抛出，使上下文根本不存在。
        assert exc.__context__ is None
        assert _SECRET not in repr(exc.__dict__)
    else:
        pytest.fail("expected ConfigError")


def test_import_does_not_load_settings() -> None:
    """模块导入时不得加载配置，否则任何 import 都会因缺变量而崩。"""
    r = subprocess.run(
        [sys.executable, "-c", "import xiaowei_agent.config"],
        env={k: v for k, v in os.environ.items() if not k.upper().startswith("XIAOWEI_")},
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr.decode()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("XIAOWEI_POSTGRES_PORT", "0"),
        ("XIAOWEI_DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS", "0"),
        ("XIAOWEI_DB_POOL_SIZE", "0"),
        ("XIAOWEI_DB_POOL_MAX_OVERFLOW", "-1"),
        ("XIAOWEI_API_BIND_HOST", "not-an-ip"),
        ("XIAOWEI_API_BIND_PORT", "65536"),
    ],
)
def test_m5_database_and_bind_boundaries_fail_closed(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev", name: value})


def test_database_timeouts_must_fit_the_command_and_lease_windows() -> None:
    with pytest.raises(ConfigError):
        load_settings(
            {
                "XIAOWEI_ENVIRONMENT_ID": "dev",
                "XIAOWEI_DB_CONNECT_TIMEOUT_SECONDS": "15",
                "XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS": "15",
            }
        )
    with pytest.raises(ConfigError):
        load_settings(
            {
                "XIAOWEI_ENVIRONMENT_ID": "dev",
                "XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS": "60",
            }
        )

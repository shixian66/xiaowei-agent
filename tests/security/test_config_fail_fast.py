"""配置 fail-fast 与不泄漏取值的承重测试。"""

import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from xiaowei_agent.config import ConfigError, load_settings

pytestmark = pytest.mark.security

_SECRET = "SUPER-" + "SECRET-" + "VALUE-XYZ"


def _test_readonly_env() -> dict[str, str]:
    return {
        "XIAOWEI_ENVIRONMENT_ID": "test",
        "XIAOWEI_ACTOR": "m6b-operator",
        "XIAOWEI_STARROCKS_ADAPTER_MODE": "test_readonly",
        "XIAOWEI_STARROCKS_HOST": "starrocks.test.invalid",
        "XIAOWEI_STARROCKS_PORT": "9030",
        "XIAOWEI_STARROCKS_DATABASE": "audit_db",
        "XIAOWEI_STARROCKS_USER": "audit_reader",
        "XIAOWEI_STARROCKS_PASSWORD_FILE": "/approved/database-credential",
        "XIAOWEI_STARROCKS_TLS_MODE": "verify_identity",
        "XIAOWEI_STARROCKS_CA_FILE": "/approved/ca.pem",
        "XIAOWEI_STARROCKS_SERVER_NAME": "starrocks.test.invalid",
        "XIAOWEI_STARROCKS_RESOURCE_ID": "approved-test-cluster",
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


@pytest.mark.parametrize(
    "missing",
    sorted(set(_test_readonly_env()) - {"XIAOWEI_ENVIRONMENT_ID", "XIAOWEI_ACTOR"}),
)
def test_starrocks_test_readonly_rejects_every_missing_required_field(missing: str) -> None:
    env = _test_readonly_env()
    del env[missing]

    with pytest.raises(ConfigError):
        load_settings(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("XIAOWEI_ENVIRONMENT_ID", "dev"),
        ("XIAOWEI_ACTOR", "another-operator"),
        ("XIAOWEI_STARROCKS_PORT", "0"),
        ("XIAOWEI_STARROCKS_TLS_MODE", "disabled"),
        ("XIAOWEI_STARROCKS_EXPECTED_DDL_SHA256", "not-a-digest"),
        ("XIAOWEI_STARROCKS_READ_TIMEOUT_SECONDS", "31"),
        ("XIAOWEI_STARROCKS_QUERY_TIMEOUT_SECONDS", "26"),
        ("XIAOWEI_STARROCKS_CONNECT_TIMEOUT_SECONDS", "5.5"),
        ("XIAOWEI_STARROCKS_HOST", "other-starrocks.test.invalid"),
        ("XIAOWEI_STARROCKS_ACTIVE_FROM", "2026-09-07T12:00:00+08:00"),
        ("XIAOWEI_STARROCKS_ACTIVE_UNTIL", "2026-09-07T08:00:00+08:00"),
    ],
)
def test_starrocks_test_readonly_rejects_unsafe_configuration(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ConfigError):
        load_settings({**_test_readonly_env(), name: value})


def test_starrocks_test_readonly_requires_timezone_aware_window() -> None:
    with pytest.raises(ConfigError):
        load_settings(
            {
                **_test_readonly_env(),
                "XIAOWEI_STARROCKS_ACTIVE_FROM": "2026-09-07T09:00:00",
            }
        )


def test_plaintext_starrocks_password_variable_is_rejected_without_leaking() -> None:
    secret = "test-" + "credential-value"
    with pytest.raises(ConfigError) as exc:
        load_settings({**_test_readonly_env(), "XIAOWEI_STARROCKS_PASSWORD": secret})

    assert "XIAOWEI_STARROCKS_PASSWORD" in str(exc.value)
    assert secret not in str(exc.value)


def test_invalid_starrocks_value_is_absent_from_full_traceback() -> None:
    secret = "invalid-" + "credential-shaped-value"
    try:
        load_settings({**_test_readonly_env(), "XIAOWEI_STARROCKS_HOST": secret + " "})
    except ConfigError as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert secret not in rendered
        assert exc.__context__ is None
    else:
        pytest.fail("expected ConfigError")


def test_starrocks_settings_validation_does_not_read_password_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "not-created"
    settings = load_settings(
        {
            **_test_readonly_env(),
            "XIAOWEI_STARROCKS_PASSWORD_FILE": str(missing_path),
        }
    )

    assert settings.starrocks_password_file == str(missing_path)

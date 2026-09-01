"""日志脱敏承重测试。

所有假密钥均在运行时拼装，源码不含完整字面量，避免被本仓库的凭证扫描命中。
"""

import io
import logging

import pytest

from xiaowei_agent.config import load_settings
from xiaowei_agent.log import LOGGER_NAME, REDACTED, configure_logging, redact

pytestmark = pytest.mark.security

_PW = "hunter" + "2-plain"
_TOKEN = "gh" + "p_" + "A" * 36
_CONN = "{}://{}:{}@{}/db".format("postgresql", "user", "p" + "wd123", "dbhost")
_BEARER_VALUE = "eyJ" + "abc123def456"


def _emit(msg: str, **extra: object) -> str:
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info(msg, extra=extra or None)
    return buf.getvalue()


def test_redact_by_key_name() -> None:
    out = redact({"password": _PW, "keep": "visible"})
    assert out == {"password": REDACTED, "keep": "visible"}


def test_redact_is_recursive_and_does_not_mutate_input() -> None:
    original = {"a": [{"api_key": _TOKEN}], "b": ("token", _TOKEN)}
    snapshot = repr(original)
    out = redact(original)
    assert _TOKEN not in repr(out)
    assert repr(original) == snapshot, "redact 不得修改入参"


def test_redact_connection_string_value_shape() -> None:
    assert "p" + "wd123" not in str(redact({"dsn": _CONN}))


@pytest.mark.parametrize("key", ["password", "token", "api_key", "authorization",
                                 "credential", "cookie", "connection_string", "conn_str"])
def test_text_key_value_pairs_are_redacted(key: str) -> None:
    out = _emit(f"failed with {key}={_PW} tail")
    assert _PW not in out
    assert REDACTED in out


def test_authorization_bearer_value_is_redacted_not_just_the_scheme() -> None:
    out = _emit(f"authorization=Bearer {_BEARER_VALUE}")
    assert _BEARER_VALUE not in out


def test_message_args_are_redacted() -> None:
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("token=%s", _TOKEN)
    assert _TOKEN not in buf.getvalue()


def test_extras_are_redacted() -> None:
    assert _PW not in _emit("ctx", password=_PW)


def test_exception_text_is_redacted() -> None:
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    try:
        raise RuntimeError(f"secret={_PW}")
    except RuntimeError:
        logging.getLogger(LOGGER_NAME).exception("boom")
    assert _PW not in buf.getvalue()


def test_stack_info_is_redacted() -> None:
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info(f"token={_TOKEN}", stack_info=True)
    assert _TOKEN not in buf.getvalue()


def test_does_not_propagate_to_host_handlers() -> None:
    """未脱敏记录不得先经 root handler 输出。"""
    host = io.StringIO()
    host_handler = logging.StreamHandler(host)
    root = logging.getLogger()
    root.addHandler(host_handler)
    prior = len(root.handlers)
    try:
        _emit(f"password={_PW}")
        assert _PW not in host.getvalue()
        assert host.getvalue() == ""
        assert logging.getLogger(LOGGER_NAME).propagate is False
        assert len(root.handlers) == prior, "不得删除或改动宿主 handler"
    finally:
        root.removeHandler(host_handler)

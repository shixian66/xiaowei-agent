"""日志脱敏承重测试。

所有假密钥均在运行时拼装，源码不含完整字面量，避免被本仓库的凭证扫描命中。
"""

import io
import json
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


# ---------------------------------------------------------------------------
# 攻击矩阵：以下每一条都曾在 c90cabb 上真实泄漏，作为回归锁保留。
# ---------------------------------------------------------------------------

_BASIC = "dXNlcjpwYXNz" + "d29yZDEyMw"


class _LeakyObject:
    """__str__ 中带密钥；redact 必须先字符串化再脱敏，不能原样放行。"""

    def __str__(self) -> str:
        return f"password={_PW}"


def test_basic_authorization_scheme_is_redacted() -> None:
    assert _BASIC not in _emit(f"Authorization: Basic {_BASIC}")


def test_quoted_json_style_key_is_redacted() -> None:
    assert _PW not in _emit(f'{{"password": "{_PW}"}}')


def test_single_quoted_key_is_redacted() -> None:
    assert _PW not in _emit(f"{{'password': '{_PW}'}}")


def test_mapping_passed_as_format_argument_is_redacted() -> None:
    """args 必须在 % 格式化之前按结构脱敏。"""
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("payload=%s", {"password": _PW})
    assert _PW not in buf.getvalue()


def test_unquoted_value_containing_spaces_is_redacted_to_delimiter() -> None:
    assert _PW not in _emit(f"password=prefix {_PW} suffix")


def test_unknown_object_in_extras_cannot_bypass_via_str() -> None:
    assert _PW not in _emit("ctx", payload=_LeakyObject())


def test_non_json_mapping_key_does_not_drop_the_record() -> None:
    """非法 JSON 键曾让 formatter 抛 TypeError 并丢失整条记录。"""
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("ctx", extra={"nested": {(1, 2): "x"}})
    assert buf.getvalue().strip(), "记录不得因序列化失败而丢失"
    assert json.loads(buf.getvalue().strip())["message"] == "ctx"


def test_foreign_handler_on_project_logger_receives_redacted_record() -> None:
    """脱敏必须发生在记录进入任何 handler 之前，而不是自有 formatter 内。"""
    foreign = io.StringIO()
    handler = logging.StreamHandler(foreign)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(LOGGER_NAME)
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=io.StringIO())
    logger.addHandler(handler)
    try:
        configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=io.StringIO())
        logger.info(f"password={_PW}")
        assert _PW not in foreign.getvalue()
        assert REDACTED in foreign.getvalue()
    finally:
        logger.removeHandler(handler)


def test_exc_info_is_cleared_so_handlers_cannot_reformat_raw_text() -> None:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(LOGGER_NAME)
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=io.StringIO())
    capture = _Capture()
    logger.addHandler(capture)
    try:
        configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=io.StringIO())
        try:
            raise RuntimeError(f"password={_PW}")
        except RuntimeError:
            logger.exception("boom")
    finally:
        logger.removeHandler(capture)
    assert records
    assert records[0].exc_info is None, "exc_info 必须清空，否则 handler 可重新格式化出原文"
    assert _PW not in (records[0].exc_text or "")


def test_format_placeholder_is_not_destroyed_by_redaction() -> None:
    """曾因先脱敏 msg 把 %s 占位符替换掉，导致 msg % args 抛 TypeError 丢失记录。"""
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("token=%s tail", _TOKEN)
    out = buf.getvalue()
    assert out.strip(), "记录不得因格式化失败而丢失"
    assert _TOKEN not in out
    assert json.loads(out.strip())["message"].startswith("token=")


def test_unquoted_sensitive_value_over_redacts_to_delimiter() -> None:
    """明确契约：未加引号的敏感值脱敏到分隔符或行尾，宁可过度脱敏也不泄漏。"""
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("token=%s tail, user=bob", _TOKEN)
    message = json.loads(buf.getvalue().strip())["message"]
    assert _TOKEN not in message
    assert "tail" not in message, "同段内的尾随文本一并脱敏（有意的过度脱敏）"
    assert "user=bob" in message, "逗号分隔的后续字段不受影响"


def test_filter_is_idempotent_across_logger_and_handler() -> None:
    """filter 同时装在 logger 与 handler 上，重复执行不得损坏记录。"""
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("a=%s b=%s", "x", "y")
    assert json.loads(buf.getvalue().strip())["message"] == "a=x b=y"

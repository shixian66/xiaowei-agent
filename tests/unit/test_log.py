"""JSON 日志的字段形状与 configure_logging 的幂等性。"""

import io
import json
import logging

from xiaowei_agent.config import load_settings
from xiaowei_agent.log import LOGGER_NAME, configure_logging
from xiaowei_agent.trace import bind_trace_id

_SETTINGS = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"})


def _one_record(msg: str = "hello") -> dict[str, object]:
    buf = io.StringIO()
    configure_logging(_SETTINGS, stream=buf)
    logging.getLogger(LOGGER_NAME).info(msg)
    return json.loads(buf.getvalue().strip())


def test_minimum_fields() -> None:
    rec = _one_record()
    assert set(rec) >= {"ts", "level", "logger", "message", "trace_id"}
    assert rec["level"] == "INFO"
    assert rec["logger"] == LOGGER_NAME
    assert rec["message"] == "hello"


def test_timestamp_is_utc_iso8601() -> None:
    assert str(_one_record()["ts"]).endswith("+00:00")


def test_trace_id_placeholder_when_unbound() -> None:
    assert _one_record()["trace_id"] == "-"


def test_trace_id_is_injected_when_bound() -> None:
    buf = io.StringIO()
    configure_logging(_SETTINGS, stream=buf)
    with bind_trace_id("b" * 32):
        logging.getLogger(LOGGER_NAME).info("x")
    assert json.loads(buf.getvalue().strip())["trace_id"] == "b" * 32


def test_configure_logging_is_idempotent() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for _ in range(3):
        configure_logging(_SETTINGS, stream=io.StringIO())
    assert len(logger.handlers) == 1


def test_log_level_is_applied() -> None:
    buf = io.StringIO()
    configure_logging(load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev",
                                     "XIAOWEI_LOG_LEVEL": "WARNING"}), stream=buf)
    logging.getLogger(LOGGER_NAME).info("suppressed")
    assert buf.getvalue() == ""

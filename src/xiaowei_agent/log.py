"""结构化 JSON 日志与脱敏。

三层脱敏规则并存：

1. **key 名**：映射键名命中敏感词即整值替换。
2. **文本键值**：自由文本中的 ``password=xxx`` / ``authorization: Bearer xxx``。
3. **value 形状**：连接串、常见 token 前缀。

覆盖 message、格式化参数、结构化 extras、异常文本与 stack_info。
只管理本项目命名 logger 并关闭 propagate，避免未脱敏记录先经宿主 handler 输出。
"""

import datetime as _dt
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Final, TextIO

from xiaowei_agent.config import Settings
from xiaowei_agent.trace import get_trace_id

LOGGER_NAME: Final[str] = "xiaowei_agent"
REDACTED: Final[str] = "***"
_MAX_DEPTH: Final[int] = 6
_HANDLER_TAG: Final[str] = "_xiaowei_owned"

_SENSITIVE_WORDS: Final[str] = (
    r"password|passwd|secret|token|api[_-]?key|authorization|credential|cookie"
    r"|connection_string|conn_str"
)
_KEY_RE: Final[re.Pattern[str]] = re.compile(_SENSITIVE_WORDS, re.IGNORECASE)
_TEXT_PAIR_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?i)\b({_SENSITIVE_WORDS})\b(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|(?:Bearer|Token)\s+\S+|\S+)"
)
_VALUE_SHAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+:[^\s/@]+@\S+"
    r"|\b(?:sk-|ghp_|gho_|AKIA)[A-Za-z0-9_\-]{8,}"
)

_RESERVED: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _scrub_text(text: str) -> str:
    text = _TEXT_PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return _VALUE_SHAPE_RE.sub(REDACTED, text)


def redact(value: object, *, _depth: int = 0) -> object:
    """返回脱敏后的**新**对象；不修改入参。超过深度上限返回 :data:`REDACTED`。"""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            k: REDACTED
            if isinstance(k, str) and _KEY_RE.search(k)
            else redact(v, _depth=_depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, (bytes, bytearray)):
        return REDACTED
    if isinstance(value, Sequence):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value


class TraceIdFilter(logging.Filter):
    """把当前 trace_id 注入 LogRecord；未绑定时为 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True


class RedactingJsonFormatter(logging.Formatter):
    """输出固定字段的单行 JSON，并对全部文本部分做脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _scrub_text(record.getMessage()),
            "trace_id": getattr(record, "trace_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = _scrub_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = _scrub_text(self.formatStack(record.stack_info))
        skip = _RESERVED | {"trace_id"}
        extras = {k: v for k, v in record.__dict__.items() if k not in skip}
        if extras:
            payload["extras"] = redact(extras)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """配置本项目命名 logger。幂等；不触碰宿主与 root handler。"""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        logger.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(stream)
    setattr(handler, _HANDLER_TAG, True)
    handler.setFormatter(RedactingJsonFormatter())
    handler.addFilter(TraceIdFilter())
    logger.addHandler(handler)
    logger.setLevel(settings.log_level)
    logger.propagate = False

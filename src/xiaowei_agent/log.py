"""结构化 JSON 日志与脱敏。

**净化位置**：脱敏由 :class:`RedactingFilter` 在 ``LogRecord`` 进入**任何** handler
之前就地完成，而不是在自有 formatter 内。原因是 formatter 只对自有 handler 生效，
挂在同一 logger 上的外部 handler 会读到未处理的记录。因此本模块：

- 把 filter 同时装到项目 logger 与其上的**每一个** handler（含外部 handler）；
- 不删除外部 handler，但会为其加装同一个 filter；
- ``propagate = False``，记录不进入 root。

**净化范围**：``msg``、``args``（在 ``%`` 格式化**之前**按结构脱敏）、自定义 extras、
异常文本与 ``stack_info``。异常以脱敏后的 ``exc_text`` 提供，并清空 ``exc_info``，
防止任何 handler 重新格式化出原始文本。

**过度脱敏优于泄漏**：未加引号的敏感值一律脱敏到分隔符或行尾。
"""

import datetime as _dt
import json
import logging
import re
import traceback
from collections.abc import Mapping, Sequence
from typing import Final, TextIO

from xiaowei_agent.config import Settings
from xiaowei_agent.trace import get_trace_id

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
"""``redact`` 的返回类型：保证 ``json.dumps`` 可直接序列化。"""

LOGGER_NAME: Final[str] = "xiaowei_agent"
REDACTED: Final[str] = "***"
_MAX_DEPTH: Final[int] = 6
_HANDLER_TAG: Final[str] = "_xiaowei_owned"
_FILTER_TAG: Final[str] = "_xiaowei_redacting"

_KEY_WORDS: Final[str] = (
    r"password|passwd|secret|token|api[_-]?key|apikey|authorization|auth"
    r"|credential|cookie|connection_string|conn_str|private_key|access_key"
)
_KEY_RE: Final[re.Pattern[str]] = re.compile(_KEY_WORDS, re.IGNORECASE)

# 键可带引号；值可为带引号串、认证方案+凭证、或未加引号的一段（到分隔符/行尾）。
_TEXT_PAIR_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?i)(?P<q>[\"']?)(?P<key>{_KEY_WORDS})(?P=q)(?P<sep>\s*[:=]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^,;}\]\n]+)"
)
_AUTH_SCHEME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(Basic|Bearer|Digest|Token|APIKey|Negotiate|NTLM)\s+[A-Za-z0-9+/=._~\-]{8,}"
)
_VALUE_SHAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@\S+"
    r"|\b(?:sk-|ghp_|gho_|ghs_|github_pat_|AKIA|ASIA)[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

_RESERVED: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def scrub_text(text: str) -> str:
    """对自由文本做键值、认证方案与值形状三类脱敏。"""
    text = _TEXT_PAIR_RE.sub(
        lambda m: f"{m.group('q')}{m.group('key')}{m.group('q')}{m.group('sep')}{REDACTED}", text
    )
    text = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _VALUE_SHAPE_RE.sub(REDACTED, text)


def redact(value: object, *, _depth: int = 0) -> JsonValue:
    """返回脱敏且 **JSON 可序列化** 的新对象；不修改入参。

    未知类型一律先 ``str()`` 再脱敏，避免绕过；映射的键强制转为已脱敏字符串，
    避免 ``json.dumps`` 因非法键类型抛错而丢失整条记录。
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bytes | bytearray):
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            skey = scrub_text(key) if isinstance(key, str) else scrub_text(str(key))
            out[skey] = REDACTED if _KEY_RE.search(skey) else redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, Sequence | set | frozenset):
        return [redact(v, _depth=_depth + 1) for v in value]
    return scrub_text(str(value))


class RedactingFilter(logging.Filter):
    """在记录分发给任何 handler 之前就地净化，并注入 trace_id。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        # 顺序很重要：先用已脱敏的 args 完成 % 格式化，再对成品文本脱敏，最后清空 args。
        # 若先脱敏 msg，会把 "%s" 之类占位符本身当作敏感值替换掉，
        # 导致后续 `msg % args` 抛 TypeError 并丢失记录。
        if record.args:
            if isinstance(record.args, Mapping):
                safe = redact(record.args)
                record.args = safe if isinstance(safe, dict) else ()
            else:
                record.args = tuple(redact(a) for a in record.args)
        record.msg = scrub_text(record.getMessage())
        record.args = None
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = scrub_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub_text(record.stack_info)
        skip = _RESERVED | {"trace_id"}
        for key in [k for k in record.__dict__ if k not in skip]:
            record.__dict__[key] = redact(record.__dict__[key])
        return True


class JsonFormatter(logging.Formatter):
    """输出固定字段的单行 JSON。记录到达此处时已由 filter 净化。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, JsonValue] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "-"),
        }
        if record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        skip = _RESERVED | {"trace_id"}
        extras = {k: v for k, v in record.__dict__.items() if k not in skip}
        if extras:
            payload["extras"] = redact(extras)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _install_filter(target: logging.Logger | logging.Handler) -> None:
    if not any(getattr(f, _FILTER_TAG, False) for f in target.filters):
        redacting = RedactingFilter()
        setattr(redacting, _FILTER_TAG, True)
        target.addFilter(redacting)


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """配置项目 logger。幂等；不删除外部 handler，但为其加装同一净化 filter。"""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        logger.removeHandler(handler)
        handler.close()
    owned = logging.StreamHandler(stream)
    setattr(owned, _HANDLER_TAG, True)
    owned.setFormatter(JsonFormatter())
    logger.addHandler(owned)
    _install_filter(logger)
    for handler in logger.handlers:
        _install_filter(handler)
    logger.setLevel(settings.log_level)
    logger.propagate = False

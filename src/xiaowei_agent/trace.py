"""请求级 trace_id 的生成与上下文绑定。

使用 :class:`contextvars.ContextVar`，因此 asyncio 任务之间天然隔离，
不需要显式传参即可让日志 filter 取到当前值。
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

_TRACE_ID: ContextVar[str | None] = ContextVar("xiaowei_trace_id", default=None)
_TRACE_ID_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{32}\Z")


def new_trace_id() -> str:
    """生成一个新的 trace_id；**不做绑定**。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    """返回当前上下文绑定的 trace_id；未绑定时为 ``None``。"""
    return _TRACE_ID.get()


@contextmanager
def bind_trace_id(trace_id: str | None = None) -> Iterator[str]:
    """在当前上下文绑定 trace_id，退出时恢复先前值（支持嵌套）。

    :param trace_id: 32 位小写十六进制字符串；``None`` 时自动生成。
    :raises ValueError: trace_id 格式非法。
    """
    value = new_trace_id() if trace_id is None else trace_id
    if not _TRACE_ID_RE.match(value):
        raise ValueError("trace_id must be 32 lowercase hex characters")
    token = _TRACE_ID.set(value)
    try:
        yield value
    finally:
        _TRACE_ID.reset(token)

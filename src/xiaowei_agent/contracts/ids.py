"""Shared identifier domains for cross-contract references."""

import re
from typing import Annotated, Final

from pydantic import AfterValidator

from xiaowei_agent.contracts.base import StrictStr

TASK_ID_MAX_LENGTH: Final[int] = 200
TASK_ID_PATTERN: Final[str] = (
    rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{TASK_ID_MAX_LENGTH - 1}}}"
)
_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(TASK_ID_PATTERN)


def _task_id(value: str) -> str:
    if _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError("task_id has an invalid shape")
    return value


TaskId = Annotated[StrictStr, AfterValidator(_task_id)]
"""任务引用的统一字符串域；与 Web path selector 使用同一完整匹配规则。"""

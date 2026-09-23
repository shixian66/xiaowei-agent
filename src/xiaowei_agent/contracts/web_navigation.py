"""Web 登录完成后由服务端重建目标所需的最小闭集数据。"""

import re
from typing import Annotated, Self, TypeAlias

from pydantic import AfterValidator, model_validator

from xiaowei_agent.contracts.base import Contract
from xiaowei_agent.contracts.enums import WebReturnIntentKind
from xiaowei_agent.contracts.identity import BoundedId
from xiaowei_agent.contracts.ids import TaskId

_ACTIVATION_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


def _closed_reference(value: str) -> str:
    """拒绝把 URL、路径、查询或控制字符伪装成申请 ID。"""
    if _ACTIVATION_REFERENCE_RE.fullmatch(value) is None:
        raise ValueError("return intent reference has an invalid shape")
    return value


ActivationRequestReference: TypeAlias = Annotated[
    BoundedId, AfterValidator(_closed_reference)
]


class WebReturnIntent(Contract):
    """登录目标的纯数据表达；不接受任意 redirect 字符串。"""

    kind: WebReturnIntentKind
    task_id: TaskId | None = None
    request_id: ActivationRequestReference | None = None

    @model_validator(mode="after")
    def _references_match_kind(self) -> Self:
        requires_task = self.kind is WebReturnIntentKind.SAFE_TASK_DETAIL
        requires_request = self.kind is WebReturnIntentKind.ACTIVATION_STATUS
        if (self.task_id is not None) is not requires_task:
            raise ValueError("return intent task reference does not match its kind")
        if (self.request_id is not None) is not requires_request:
            raise ValueError("return intent request reference does not match its kind")
        return self


__all__ = [
    "ActivationRequestReference",
    "WebReturnIntent",
    "WebReturnIntentKind",
]

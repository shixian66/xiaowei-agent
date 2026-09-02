"""恢复时由外部带入的输入。

``user_text`` 是 ``ExternalContent``，因此用户补充的内容恒为不可信外部文本，不能
改变 policy、目标、权限、审批状态或执行计划。

**字段组合按 kind 封闭**：这是 Runner resume 的跨边界输入，半空或自相矛盾的对象
不能留给下游去猜。审批恢复只允许 approval ref；用户补充只允许不可信外部文本；
"无外部输入"用 ``None`` 表示，不用一个两个字段都空的对象。
"""

from typing import Self

from pydantic import model_validator

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import ExternalInputKind
from xiaowei_agent.contracts.external import ExternalContent


class ExternalInput(Contract):
    kind: ExternalInputKind
    approval_ref: StrictStr | None = None
    user_text: ExternalContent | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> Self:
        if self.kind is ExternalInputKind.APPROVAL_DECISION:
            if self.approval_ref is None:
                raise ValueError("an approval decision requires approval_ref")
            if self.user_text is not None:
                raise ValueError("an approval decision must not carry user_text")
        else:
            if self.user_text is None:
                raise ValueError("a user supplement requires user_text")
            if self.approval_ref is not None:
                raise ValueError("a user supplement must not carry approval_ref")
        return self

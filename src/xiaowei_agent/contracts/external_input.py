"""恢复时由外部带入的输入。

``user_text`` 是 ``ExternalContent``，因此用户补充的内容恒为不可信外部文本，不能
改变 policy、目标、权限、审批状态或执行计划。
"""

from xiaowei_agent.contracts.base import Contract
from xiaowei_agent.contracts.enums import ExternalInputKind
from xiaowei_agent.contracts.external import ExternalContent


class ExternalInput(Contract):
    kind: ExternalInputKind
    approval_ref: str | None = None
    user_text: ExternalContent | None = None

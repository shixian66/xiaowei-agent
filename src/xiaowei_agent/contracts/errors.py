"""结构化错误模型。

**不含自由文本消息**：``message_key`` 是 i18n 键，``cause_ref`` 是指向
``ExternalContent.digest`` 或 trace 事件 id 的引用。第三方错误原文只能经
``ExternalContent`` 承载，再由确定性 mapper 归类，不作为控制信号。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import ErrorCategory


class AgentError(Contract):
    code: StrictStr
    category: ErrorCategory
    retryable: bool
    message_key: StrictStr
    cause_ref: StrictStr | None = None

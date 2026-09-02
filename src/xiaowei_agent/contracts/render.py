"""跨渠道统一回答投影。

Web、飞书、CLI 只选择展示方式，不重算业务结果——因此本契约**不含**任何可让渠道
推导业务判断的字段（无原始 rows、无 SQL、无 policy 细节）。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import TaskStatus


class RenderSection(Contract):
    title: StrictStr
    body: str
    refs: tuple[StrictStr, ...]


class RenderPayload(Contract):
    answer: str
    sections: tuple[RenderSection, ...]
    next_steps: tuple[str, ...]
    status: TaskStatus
    refs: tuple[StrictStr, ...]

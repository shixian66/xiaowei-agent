"""入口信封与执行上下文。

``RequestEnvelope`` 是渠道递交的原始请求；``RequestContext`` 是 Gateway 解析后
产生的执行上下文，**五项全必填**——环境无法解析出唯一值时 fail-closed，不取默认
环境（ADR-007 D2）。模块边界显式传递 ``RequestContext``，不从全局变量读取。
"""

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, StrictStr, TraceId
from xiaowei_agent.contracts.enums import Channel


class RequestEnvelope(Contract):
    request_id: StrictStr
    tenant_id: StrictStr
    actor: StrictStr
    channel: Channel
    text: str = Field(max_length=8192)
    idempotency_key: StrictStr
    environment_id: str | None = None


class RequestContext(Contract):
    tenant_id: StrictStr
    actor: StrictStr
    environment_id: StrictStr
    trace_id: TraceId
    policy_revision: StrictStr

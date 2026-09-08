"""M7 渠道身份与安全投影输入契约。

这些类型只表达服务端已认证身份和展示输入；不携带角色推断、业务路由、执行计划、
原始证据或工具结果。
"""

from pydantic import AnyHttpUrl, Field

from xiaowei_agent.contracts.base import Contract, StrictInt, StrictStr
from xiaowei_agent.contracts.enums import ChannelPermission, IdentitySource
from xiaowei_agent.contracts.task import TaskView


class AuthenticatedPrincipal(Contract):
    """由服务端身份目录构造的渠道访问主体。"""

    tenant_id: StrictStr
    environment_id: StrictStr
    actor: StrictStr
    source: IdentitySource
    subject_ref: StrictStr
    permissions: frozenset[ChannelPermission]


class FeishuProjectionInput(Contract):
    """飞书卡片唯一允许消费的安全任务投影。"""

    task_view: TaskView
    request_preview: StrictStr
    task_version: StrictInt = Field(ge=0)
    detail_url: AnyHttpUrl

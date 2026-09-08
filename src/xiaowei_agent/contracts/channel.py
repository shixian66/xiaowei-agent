"""M7 渠道身份与安全投影输入契约。

这些类型只表达服务端已认证身份和展示输入；不携带角色推断、业务路由、执行计划、
原始证据或工具结果。
"""

from typing import Annotated, TypeAlias

from pydantic import AfterValidator, AnyHttpUrl, Field

from xiaowei_agent.contracts.base import Contract, NonEmptyText, StrictInt, StrictStr
from xiaowei_agent.contracts.enums import ChannelPermission, IdentitySource
from xiaowei_agent.contracts.task import TaskView


def _require_https(value: AnyHttpUrl) -> AnyHttpUrl:
    if value.scheme != "https":
        raise ValueError("detail URL must use https")
    return value


HttpsUrl: TypeAlias = Annotated[AnyHttpUrl, AfterValidator(_require_https)]


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
    request_preview: NonEmptyText = Field(max_length=8192)
    task_version: StrictInt = Field(ge=0)
    detail_url: HttpsUrl

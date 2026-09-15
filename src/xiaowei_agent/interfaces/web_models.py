"""Web 工作台的严格请求与安全响应模型。"""

from typing import Annotated, Self, TypeAlias
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xiaowei_agent.application.channel_access import (
    TASK_DETAIL_PREVIEW_LIMIT,
    TASK_SUMMARY_PREVIEW_LIMIT,
    AccessibleTask,
    TaskPage,
    TaskSummary,
)
from xiaowei_agent.application.channel_submission import SubmittedTask
from xiaowei_agent.application.integration_state import ProviderDisplayState
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    AwareDatetime,
    ChannelPermission,
    NonEmptyText,
    ProviderName,
    RenderPayload,
    SecretRef,
    StrictInt,
    StrictStr,
    TaskId,
    TaskStatus,
)

SecretPassword: TypeAlias = Annotated[StrictStr, Field(min_length=1, max_length=256)]
"""**提交上来的**明文口令；只做长度边界，不判强度。

名字以 ``Secret`` 开头是有意义的：凡是这样标注的字段都必须同时写
``exclude=True`` 与 ``repr=False``（见 ``tests/security/test_secret_field_exposure.py``）。

登录这一侧**不能**设下限：seed 写入的初始口令是 5 位的 ``admin``，在这里加最小
长度会让第一次登录直接 422，整条闭环卡死在第一步。强度要求只属于"设置新口令"
那一侧，见 :data:`SecretNewPassword`。
"""

SecretNewPassword: TypeAlias = Annotated[StrictStr, Field(min_length=12, max_length=256)]
"""**要设置成的**新口令；这里才是强度门。

12 位是下限而不是建议：初始口令是写死在源码里的常量，任何人都知道它，因此
第一次改密是这台机器上唯一一次真正建立凭据的机会。
"""


class _WebModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
    )


def web_task_detail_path(task_id: str) -> str:
    """生成同源详情路径；任务 ID 不能改变路由层级。"""
    return f"/app/tasks/{quote(task_id, safe='')}"


class WebTaskSubmitRequest(_WebModel):
    """浏览器可提交正文、幂等引用和可选显式父任务。"""

    text: NonEmptyText = Field(max_length=8192)
    client_submission_id: StrictStr = Field(
        min_length=16,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    parent_task_id: TaskId | None = None


class WebLoginRequest(_WebModel):
    """本地管理员登录请求。

    ``password`` 标为 ``Secret*`` 并同时关掉 ``repr`` 与 ``model_dump``：请求体
    模型最容易在校验失败时被打进日志，明文口令不能走那条路。
    """

    password: SecretPassword = Field(exclude=True, repr=False)


class WebChangePasswordRequest(_WebModel):
    """改密请求；两个字段都是明文口令。"""

    current_password: SecretPassword = Field(exclude=True, repr=False)
    new_password: SecretNewPassword = Field(exclude=True, repr=False)


ConfigText: TypeAlias = Annotated[StrictStr, Field(min_length=1, max_length=256)]
"""配置里的非 secret 文本（目前只有飞书 App ID）。

下限是 1 而不是 0：空串在这条链路上唯一可能的含义是"我想清掉它"，而清除必须走
显式动作。留出空串等于给出第二种清除语义，而它不会触发任何确认。
"""


class _ConfigUpdate(_WebModel):
    """保存请求里"未携带 = 保留原值"这条语义的共同约束。

    显式 ``null`` 与空串一样被拒绝：两者都是"用一个取值暗示清除"，而清除只能走
    ``POST /app/api/config/clear``。规则写在基类上，新增字段自动继承——写在每个
    字段上迟早会漏掉一个。
    """

    @model_validator(mode="after")
    def _an_explicit_null_is_not_a_way_to_clear(self) -> Self:
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("null does not clear a field; use the clear action")
        return self


class WebGeminiConfigUpdate(_ConfigUpdate):
    """Gemini 的可编辑字段。模型、端点、API 版本是代码常量，不在这里。"""

    enabled: bool | None = None
    api_key: SecretRef | None = Field(default=None, exclude=True, repr=False)


class WebFeishuConfigUpdate(_ConfigUpdate):
    """飞书的可编辑字段。"""

    enabled: bool | None = None
    app_id: ConfigText | None = None
    app_secret: SecretRef | None = Field(default=None, exclude=True, repr=False)


class WebConfigUpdateRequest(_ConfigUpdate):
    """一次保存请求；两个 Provider 都可以整段不携带。"""

    gemini: WebGeminiConfigUpdate | None = None
    feishu: WebFeishuConfigUpdate | None = None


class WebConfigClearRequest(_WebModel):
    """显式清除某个 Provider 的全部配置。"""

    # 闭集仍然是 ``ProviderName``；只在这一个字段上放开 ``strict``。模型整体
    # ``strict=True`` 时枚举只接受枚举实例，而请求体里来的必然是 JSON 字符串——
    # 不放开就没有任何合法请求。改用 ``Literal`` 会把同一个闭集抄成第二份。
    provider: ProviderName = Field(strict=False)


class WebGeminiConfigView(_WebModel):
    """Gemini 的查询投影；``configured`` 是布尔，Key 永不回显。"""

    enabled: bool
    configured: bool


class WebFeishuConfigView(_WebModel):
    """飞书的查询投影；``app_id`` 不是 secret，页面要靠它确认填的是哪个应用。"""

    enabled: bool
    configured: bool
    app_id: StrictStr | None


class WebConfigChecks(_WebModel):
    """三个测试项的页面状态。

    写成三个字段而不是一个 ``dict``：测试项是闭集，模型本身就是那份闭集，
    多一项少一项都会在这里变红。
    """

    gemini_connection: ProviderDisplayState
    feishu_credentials: ProviderDisplayState
    feishu_oauth: ProviderDisplayState


class WebConfigView(_WebModel):
    """``GET /app/api/config`` 的完整响应。

    ``generation`` 为 ``0`` 表示文件尚不存在——干净部署的正常起点，不是错误。
    """

    generation: StrictInt
    gemini: WebGeminiConfigView
    feishu: WebFeishuConfigView
    checks: WebConfigChecks


class WebConfigSaved(_WebModel):
    """保存与清除的成功响应。

    ``restart_required`` 恒为真：Web 只写文件，不重启任何进程，也不挂
    ``docker.sock``。配置生效需要宿主机执行一次 Compose 重启。
    """

    generation: StrictInt
    restart_required: bool


class WebOAuthTestStarted(_WebModel):
    """飞书 OAuth 连接测试**已签发**的响应；与失败响应是两种形状。

    只有这一条会带 ``authorization_url``，因此前端用它的有无来分支即可。刻意不把
    它塞进探针结果模型里做成可选字段：那会造出一个既像结果又像跳转指令的对象，
    读的人无法从类型上判断该跳转还是该显示红色徽章。
    """

    authorization_url: StrictStr


class WebCurrentUser(_WebModel):
    actor: StrictStr
    environment_id: StrictStr
    permissions: tuple[ChannelPermission, ...]
    csrf_token: StrictStr = Field(min_length=64, max_length=64, repr=False)

    @classmethod
    def from_principal(
        cls, principal: AuthenticatedPrincipal, *, csrf_token: str
    ) -> "WebCurrentUser":
        """按主体构造。

        不再直接收某一种 session 对象：本地管理员与飞书 OAuth 是两种会话形状，
        绑死其中一种会逼着另一条路径伪造一个假的。
        """
        return cls(
            actor=principal.actor,
            environment_id=principal.environment_id,
            permissions=tuple(sorted(principal.permissions, key=lambda item: item.value)),
            csrf_token=csrf_token,
        )


class WebTaskSummary(_WebModel):
    task_id: TaskId
    status: TaskStatus
    request_preview: NonEmptyText = Field(max_length=TASK_SUMMARY_PREVIEW_LIMIT)
    submitted_at: AwareDatetime
    detail_path: StrictStr

    @model_validator(mode="after")
    def _detail_path_belongs_to_task(self) -> Self:
        if self.detail_path != web_task_detail_path(self.task_id):
            raise ValueError("detail_path does not belong to task_id")
        return self

    @classmethod
    def from_summary(cls, summary: TaskSummary) -> "WebTaskSummary":
        return cls(
            task_id=summary.task_id,
            status=summary.status,
            request_preview=summary.request_preview,
            submitted_at=summary.submitted_at,
            detail_path=web_task_detail_path(summary.task_id),
        )


class WebTaskPage(_WebModel):
    items: tuple[WebTaskSummary, ...]
    next_created_seq: StrictInt | None = Field(default=None, gt=0)

    @classmethod
    def from_page(cls, page: TaskPage) -> "WebTaskPage":
        return cls(
            items=tuple(WebTaskSummary.from_summary(item) for item in page.items),
            next_created_seq=page.next_created_seq,
        )


class WebTaskAccepted(_WebModel):
    task_id: TaskId
    status: TaskStatus
    detail_path: StrictStr
    parent_task_id: TaskId | None = None

    @model_validator(mode="after")
    def _detail_path_belongs_to_task(self) -> Self:
        if self.detail_path != web_task_detail_path(self.task_id):
            raise ValueError("detail_path does not belong to task_id")
        return self

    @classmethod
    def from_submission(cls, submission: SubmittedTask) -> "WebTaskAccepted":
        view = submission.task_view
        return cls(
            task_id=view.task_id,
            status=view.status,
            detail_path=web_task_detail_path(view.task_id),
            parent_task_id=submission.parent_task_id,
        )


class WebTaskDetail(_WebModel):
    task_id: TaskId
    status: TaskStatus
    request_preview: NonEmptyText = Field(max_length=TASK_DETAIL_PREVIEW_LIMIT)
    submitted_at: AwareDatetime
    task_version: StrictInt = Field(ge=0)
    detail_path: StrictStr
    render: RenderPayload | None = None
    parent_task_id: TaskId | None = None

    @model_validator(mode="after")
    def _detail_path_belongs_to_task(self) -> Self:
        if self.detail_path != web_task_detail_path(self.task_id):
            raise ValueError("detail_path does not belong to task_id")
        return self

    @classmethod
    def from_accessible(cls, accessible: AccessibleTask) -> "WebTaskDetail":
        view = accessible.task_view
        return cls(
            task_id=view.task_id,
            status=view.status,
            request_preview=accessible.request_preview,
            submitted_at=accessible.submitted_at,
            task_version=accessible.task_version,
            detail_path=web_task_detail_path(view.task_id),
            render=view.render,
            parent_task_id=accessible.parent_task_id,
        )


__all__ = [
    "SecretNewPassword",
    "SecretPassword",
    "WebChangePasswordRequest",
    "WebCurrentUser",
    "WebLoginRequest",
    "WebOAuthTestStarted",
    "WebTaskAccepted",
    "WebTaskDetail",
    "WebTaskPage",
    "WebTaskSubmitRequest",
    "WebTaskSummary",
    "web_task_detail_path",
]

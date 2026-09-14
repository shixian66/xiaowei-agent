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
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    AwareDatetime,
    ChannelPermission,
    NonEmptyText,
    RenderPayload,
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
    task_id: StrictStr
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
    task_id: StrictStr
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
    task_id: StrictStr
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
    "WebTaskAccepted",
    "WebTaskDetail",
    "WebTaskPage",
    "WebTaskSubmitRequest",
    "WebTaskSummary",
    "web_task_detail_path",
]

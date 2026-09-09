"""Web 工作台的严格请求与安全响应模型。"""

from typing import Self
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
    AwareDatetime,
    ChannelPermission,
    NonEmptyText,
    RenderPayload,
    StrictInt,
    StrictStr,
    TaskStatus,
)
from xiaowei_agent.interfaces.web_auth import AuthenticatedWebSession


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
    """浏览器唯一可提交的两个字段；身份与作用域只取服务端 session。"""

    text: NonEmptyText = Field(max_length=8192)
    client_submission_id: StrictStr = Field(
        min_length=16,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class WebCurrentUser(_WebModel):
    actor: StrictStr
    environment_id: StrictStr
    permissions: tuple[ChannelPermission, ...]
    csrf_token: StrictStr = Field(min_length=64, max_length=64, repr=False)

    @classmethod
    def from_session(cls, session: AuthenticatedWebSession) -> "WebCurrentUser":
        return cls(
            actor=session.principal.actor,
            environment_id=session.principal.environment_id,
            permissions=tuple(sorted(session.principal.permissions, key=lambda item: item.value)),
            csrf_token=session.csrf_token,
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
        )


class WebTaskDetail(_WebModel):
    task_id: StrictStr
    status: TaskStatus
    request_preview: NonEmptyText = Field(max_length=TASK_DETAIL_PREVIEW_LIMIT)
    submitted_at: AwareDatetime
    task_version: StrictInt = Field(ge=0)
    detail_path: StrictStr
    render: RenderPayload | None = None

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
        )


__all__ = [
    "WebCurrentUser",
    "WebTaskAccepted",
    "WebTaskDetail",
    "WebTaskPage",
    "WebTaskSubmitRequest",
    "WebTaskSummary",
    "web_task_detail_path",
]

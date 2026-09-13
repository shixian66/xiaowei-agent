"""Web/飞书共用的提交权限、服务端幂等与渠道绑定编排。"""

from typing import NamedTuple, Protocol, Self

from pydantic import Field, model_validator

from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    AwareDatetime,
    Channel,
    ChannelKind,
    ChannelPermission,
    Contract,
    DestinationKind,
    NonEmptyText,
    ProjectionState,
    RequestContext,
    RequestEnvelope,
    StrictStr,
    TaskSubmission,
    TaskView,
    TraceId,
    content_digest,
)
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    ChannelBinding,
    ChannelStore,
    CreateProjectionSubscriptionCommand,
)
from xiaowei_agent.planning import canonical_json


class ChannelSubmissionForbiddenError(PermissionError):
    """已认证主体缺少提交只读任务的显式权限。"""

    def __init__(self) -> None:
        super().__init__("submission forbidden")


class WebParentAccessPort(Protocol):
    """Web-only 父任务授权端口；飞书提交进程不装配该依赖。"""

    async def require_web_parent_access(
        self,
        *,
        principal: AuthenticatedPrincipal,
        parent_task_id: str,
    ) -> None:
        """仅在父链满足同 scope、身份、owner 和终态约束时返回。"""


class ChannelSubmitCommand(Contract):
    principal: AuthenticatedPrincipal
    channel: ChannelKind
    request_id: StrictStr
    trace_id: TraceId
    policy_revision: StrictStr
    text: NonEmptyText = Field(max_length=8192)
    client_submission_ref: StrictStr
    conversation_ref: StrictStr | None = None
    submitted_at: AwareDatetime
    parent_task_id: StrictStr | None = None

    @model_validator(mode="after")
    def _shape_is_supported(self) -> Self:
        if self.channel is ChannelKind.FEISHU_GROUP and self.conversation_ref is None:
            raise ValueError("group submission requires conversation_ref")
        if self.parent_task_id is not None and self.channel is not ChannelKind.WEB:
            raise ValueError("parent task is only supported for web")
        return self


class SubmittedTask(Contract):
    task_view: TaskView
    binding: ChannelBinding
    parent_task_id: StrictStr | None = None


class ChannelSubmissionReferences(NamedTuple):
    """同一作用域摘要派生出的 Runtime key 与渠道来源引用。"""

    idempotency_key: str
    source_event_ref: str


def _idempotency_payload(command: ChannelSubmitCommand) -> dict[str, str]:
    principal = command.principal
    return {
        "tenant_id": principal.tenant_id,
        "environment_id": principal.environment_id,
        "channel": command.channel.value,
        "actor": principal.actor,
        "client_key_or_event_id": command.client_submission_ref,
    }


def derive_channel_submission_references(
    command: ChannelSubmitCommand,
) -> ChannelSubmissionReferences:
    """一次派生两个引用；actor 入摘要可避免跨主体误撞来源唯一约束。"""
    digest = content_digest(canonical_json(_idempotency_payload(command)).decode("utf-8"))
    return ChannelSubmissionReferences(
        idempotency_key=f"channel:v1:{digest}",
        source_event_ref=digest,
    )


def channel_idempotency_key(command: ChannelSubmitCommand) -> str:
    """按全部授权作用域维度生成 Runtime 的服务端幂等键。"""
    return derive_channel_submission_references(command).idempotency_key


def _projection_command(
    command: ChannelSubmitCommand, *, task_id: str
) -> CreateProjectionSubscriptionCommand | None:
    principal = command.principal
    if (
        command.channel is ChannelKind.WEB
        and ChannelPermission.ADMIN_ALL_SAFE_TASKS in principal.permissions
    ):
        return None
    if command.channel is ChannelKind.WEB:
        return CreateProjectionSubscriptionCommand(
            task_id=task_id,
            destination_kind=DestinationKind.FEISHU_PRIVATE_NOTICE,
            destination_ref=principal.subject_ref,
            initial_state=ProjectionState.WAITING_TERMINAL,
            next_attempt_at=command.submitted_at,
        )
    return CreateProjectionSubscriptionCommand(
        task_id=task_id,
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref=command.conversation_ref or principal.subject_ref,
        initial_state=ProjectionState.PENDING_INITIAL,
        next_attempt_at=command.submitted_at,
    )


class ChannelSubmissionService:
    """授权后复用 TaskViewRuntime，并原子恢复渠道绑定与通知订阅。"""

    def __init__(
        self,
        *,
        runtime: TaskViewRuntime,
        channel_store: ChannelStore,
        web_parent_access: WebParentAccessPort | None = None,
    ) -> None:
        self._runtime = runtime
        self._channels = channel_store
        self._web_parent_access = web_parent_access

    async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask:
        """提交任务；Runtime 成功但绑定失败时由同一服务端幂等键安全重试。"""
        principal = command.principal
        if ChannelPermission.SUBMIT_READONLY_TASK not in principal.permissions:
            raise ChannelSubmissionForbiddenError
        if command.parent_task_id is not None:
            if self._web_parent_access is None:
                raise ChannelSubmissionForbiddenError
            await self._web_parent_access.require_web_parent_access(
                principal=principal,
                parent_task_id=command.parent_task_id,
            )
        references = derive_channel_submission_references(command)
        task_view = await self._runtime.submit_task(
            submission=TaskSubmission(
                envelope=RequestEnvelope(
                    request_id=command.request_id,
                    tenant_id=principal.tenant_id,
                    actor=principal.actor,
                    channel=Channel.WEB
                    if command.channel is ChannelKind.WEB
                    else Channel.FEISHU,
                    text=command.text,
                    idempotency_key=references.idempotency_key,
                    environment_id=principal.environment_id,
                ),
                context=RequestContext(
                    tenant_id=principal.tenant_id,
                    actor=principal.actor,
                    environment_id=principal.environment_id,
                    trace_id=command.trace_id,
                    policy_revision=command.policy_revision,
                ),
                as_of=command.submitted_at,
                parent_task_id=command.parent_task_id,
            )
        )
        binding = await self._channels.bind_task(
            command=BindTaskCommand(
                task_id=task_view.task_id,
                tenant_id=principal.tenant_id,
                environment_id=principal.environment_id,
                channel=command.channel,
                initiator_subject_ref=principal.subject_ref,
                conversation_ref=command.conversation_ref,
                source_event_ref=references.source_event_ref,
                created_at=command.submitted_at,
                projection=_projection_command(command, task_id=task_view.task_id),
            )
        )
        return SubmittedTask(
            task_view=task_view,
            binding=binding,
            parent_task_id=command.parent_task_id,
        )


__all__ = [
    "ChannelSubmissionForbiddenError",
    "ChannelSubmissionReferences",
    "ChannelSubmissionService",
    "ChannelSubmitCommand",
    "SubmittedTask",
    "WebParentAccessPort",
    "channel_idempotency_key",
    "derive_channel_submission_references",
]

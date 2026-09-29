"""Web/飞书共用的提交权限、服务端幂等与渠道绑定编排。"""

from typing import Final, NamedTuple, Protocol, Self

from pydantic import model_validator

from xiaowei_agent.application.task_view_runtime import (
    RecognizedSql,
    SqlLikeTextNotAcceptedError,
    SqlMessageRecognizer,
    TaskViewRuntime,
)
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    AwareDatetime,
    Channel,
    ChannelKind,
    ChannelPermission,
    Contract,
    ConversationSubmission,
    DestinationKind,
    NonEmptyText,
    ProjectionState,
    RequestContext,
    RequestEnvelope,
    StrictStr,
    TaskId,
    TaskView,
    TraceId,
    content_digest,
)
from xiaowei_agent.persistence.channel import (
    BindTaskCommand,
    ChannelBinding,
    ChannelSourceKindConflictError,
    ChannelStore,
    ClaimSourceEventCommand,
    CreateProjectionSubscriptionCommand,
    PrivateChatLookup,
    SourceSubmissionKind,
)
from xiaowei_agent.persistence.store import (
    IdempotencyConflictError,
    SqlQuerySubmitCommand,
    TaskNotFoundError,
)
from xiaowei_agent.planning import canonical_json

MAX_CONVERSATION_CHARACTERS: Final[int] = 8192
"""普通消息上限；只有识别为 SQL 的消息才可以更长（设计 §5.1）。"""

MAX_CHANNEL_TEXT_BYTES: Final[int] = 65_536
"""渠道文本 UTF-8 上限，等于 SQL 原文上限（设计 §8.1）。"""


class ChannelSubmissionForbiddenError(PermissionError):
    """已认证主体缺少提交只读任务的显式权限。"""

    def __init__(self) -> None:
        super().__init__("submission forbidden")


class ChannelParentNotFoundError(LookupError):
    """澄清父任务不存在、无权访问或已被消费；入口统一映射为 not_found。"""

    def __init__(self) -> None:
        super().__init__("task not found")


class ChannelMessageTooLongError(ValueError):
    """不是 SQL 消息却超过普通消息上限；入口按“消息过长”拒绝。"""

    def __init__(self) -> None:
        super().__init__("message too long")


class WebParentAccessPort(Protocol):
    """Web-only 父任务授权端口；飞书提交进程不装配该依赖。"""

    async def require_web_parent_access(
        self,
        *,
        principal: AuthenticatedPrincipal,
        clarification_parent_task_id: str,
    ) -> None:
        """仅在父链满足同 scope、身份、owner 和终态约束时返回。"""


class ChannelSubmitCommand(Contract):
    principal: AuthenticatedPrincipal
    channel: ChannelKind
    request_id: StrictStr
    trace_id: TraceId
    policy_revision: StrictStr
    # 渠道文本上限按 UTF-8 bytes 计（SQL 消息最多 65_536 bytes）；普通消息的 8192 字符
    # 上限由提交服务在识别之后执行。
    text: NonEmptyText
    client_submission_ref: StrictStr
    conversation_ref: StrictStr | None = None
    # 飞书私聊事件里的 p2p 会话 id。真实飞书拒绝以 open_id 为 receive_id 发私聊
    # （230101），同一会话按 chat_id 发送才可达；它只决定回复落点，不进入绑定
    # 与群成员鉴权。
    private_chat_ref: StrictStr | None = None
    submitted_at: AwareDatetime
    clarification_parent_task_id: TaskId | None = None

    @model_validator(mode="after")
    def _shape_is_supported(self) -> Self:
        if len(self.text.encode("utf-8")) > MAX_CHANNEL_TEXT_BYTES:
            raise ValueError("channel text exceeds its byte limit")
        if self.channel is ChannelKind.FEISHU_GROUP and self.conversation_ref is None:
            raise ValueError("group submission requires conversation_ref")
        if self.channel is ChannelKind.FEISHU_PRIVATE:
            if self.private_chat_ref is None:
                raise ValueError("private submission requires private_chat_ref")
        elif self.private_chat_ref is not None:
            raise ValueError("private_chat_ref is only supported for private")
        if self.clarification_parent_task_id is not None and self.channel is not ChannelKind.WEB:
            raise ValueError("parent task is only supported for web")
        return self


class SubmittedTask(Contract):
    task_view: TaskView
    binding: ChannelBinding
    clarification_parent_task_id: TaskId | None = None


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


def _wants_web_notice(command: ChannelSubmitCommand) -> bool:
    return (
        command.channel is ChannelKind.WEB
        and ChannelPermission.ADMIN_ALL_SAFE_TASKS not in command.principal.permissions
    )


def _projection_command(
    command: ChannelSubmitCommand, *, task_id: str, web_notice_chat_ref: str | None
) -> CreateProjectionSubscriptionCommand | None:
    if command.channel is ChannelKind.WEB:
        # 真实飞书拒绝按 open_id 发私聊（230101），只能发进用户私聊小维时的 p2p
        # 会话；从没私聊过小维的用户没有可达会话，不建飞书通知，结果只在 Web 上看。
        if not _wants_web_notice(command) or web_notice_chat_ref is None:
            return None
        return CreateProjectionSubscriptionCommand(
            task_id=task_id,
            destination_kind=DestinationKind.FEISHU_PRIVATE_NOTICE,
            destination_ref=web_notice_chat_ref,
            initial_state=ProjectionState.WAITING_TERMINAL,
            next_attempt_at=command.submitted_at,
        )
    destination_ref = command.conversation_ref or command.private_chat_ref
    if destination_ref is None:  # pragma: no cover - 由命令校验器保证
        raise ValueError("feishu submission has no reply destination")
    return CreateProjectionSubscriptionCommand(
        task_id=task_id,
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref=destination_ref,
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
        """提交任务；Runtime 成功但绑定失败时由同一服务端幂等键安全重试。

        :raises SqlLikeTextNotAcceptedError: 文本像 SQL；来源事件与任务事实都还没写。
        """
        principal = command.principal
        if ChannelPermission.SUBMIT_READONLY_TASK not in principal.permissions:
            raise ChannelSubmissionForbiddenError
        if command.clarification_parent_task_id is not None:
            if self._web_parent_access is None:
                raise ChannelSubmissionForbiddenError
            await self._web_parent_access.require_web_parent_access(
                principal=principal,
                clarification_parent_task_id=command.clarification_parent_task_id,
            )
        references = derive_channel_submission_references(command)
        context = RequestContext(
            tenant_id=principal.tenant_id,
            actor=principal.actor,
            environment_id=principal.environment_id,
            trace_id=command.trace_id,
            policy_revision=command.policy_revision,
        )
        # 澄清回答只是补充信息（例如目标名称），永远不当作新的 SQL；是 SQL 形状的回答由
        # Runtime 的同一分类点拒绝，不进入模型。像 SQL 的文本在这里（占用来源事件之前）就被
        # 同一分类点拒绝，原文不落进普通对话提交。
        sql = (
            self._runtime.recognize_sql(command.text)
            if command.clarification_parent_task_id is None
            else None
        )
        if sql is None and len(command.text) > MAX_CONVERSATION_CHARACTERS:
            raise ChannelMessageTooLongError
        await self._claim_source(
            command=command,
            references=references,
            input_kind="conversation" if sql is None else "sql_artifact",
        )
        if sql is not None:
            task_view = await self._runtime.submit_sql_query(
                command=SqlQuerySubmitCommand(
                    context=context,
                    sql_bytes=sql.sql_bytes,
                    idempotency_key=references.idempotency_key,
                    as_of=command.submitted_at,
                )
            )
            return await self._bind(
                command=command, references=references, task_view=task_view
            )
        submission = ConversationSubmission(
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
            context=context,
            as_of=command.submitted_at,
            clarification_parent_task_id=command.clarification_parent_task_id,
        )
        try:
            if command.clarification_parent_task_id is None:
                task_view = await self._runtime.submit_task(submission=submission)
            else:
                task_view = await self._runtime.submit_clarification_child(
                    submission=submission,
                    authenticated_channel_owner=principal.actor,
                )
        except TaskNotFoundError:
            raise ChannelParentNotFoundError from None
        return await self._bind(
            command=command, references=references, task_view=task_view
        )

    async def _claim_source(
        self,
        *,
        command: ChannelSubmitCommand,
        references: ChannelSubmissionReferences,
        input_kind: SourceSubmissionKind,
    ) -> None:
        """创建任务之前为来源事件原子选定提交类型。

        TaskStore 让对话与 SQL 的幂等键分属不同作用域（ADR-018 D2），同一来源事件的另一
        种类型会在那里成功建出第二个任务，再在绑定时失败，留下未绑定却可调度的任务。
        占位先于任何任务事实写入，所以另一种类型在这里就被拒绝。

        :raises IdempotencyConflictError: 该来源事件已选定另一种提交类型。
        """
        principal = command.principal
        try:
            await self._channels.claim_source_event(
                command=ClaimSourceEventCommand(
                    tenant_id=principal.tenant_id,
                    environment_id=principal.environment_id,
                    source_event_ref=references.source_event_ref,
                    input_kind=input_kind,
                    created_at=command.submitted_at,
                )
            )
        except ChannelSourceKindConflictError:
            raise IdempotencyConflictError(
                "channel source event already chose another kind"
            ) from None

    async def _bind(
        self,
        *,
        command: ChannelSubmitCommand,
        references: ChannelSubmissionReferences,
        task_view: TaskView,
    ) -> SubmittedTask:
        """对话与 SQL 提交共用的渠道绑定与通知订阅；ChannelStore 不保存 SQL。"""
        principal = command.principal
        web_notice_chat_ref = None
        if _wants_web_notice(command):
            web_notice_chat_ref = await self._channels.find_private_chat_ref(
                lookup=PrivateChatLookup(
                    tenant_id=principal.tenant_id,
                    environment_id=principal.environment_id,
                    subject_ref=principal.subject_ref,
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
                projection=_projection_command(
                    command,
                    task_id=task_view.task_id,
                    web_notice_chat_ref=web_notice_chat_ref,
                ),
            )
        )
        return SubmittedTask(
            task_view=task_view,
            binding=binding,
            clarification_parent_task_id=command.clarification_parent_task_id,
        )


__all__ = [
    "MAX_CHANNEL_TEXT_BYTES",
    "MAX_CONVERSATION_CHARACTERS",
    "ChannelMessageTooLongError",
    "ChannelParentNotFoundError",
    "ChannelSubmissionForbiddenError",
    "ChannelSubmissionReferences",
    "ChannelSubmissionService",
    "ChannelSubmitCommand",
    "RecognizedSql",
    "SqlLikeTextNotAcceptedError",
    "SqlMessageRecognizer",
    "SubmittedTask",
    "WebParentAccessPort",
    "channel_idempotency_key",
    "derive_channel_submission_references",
]

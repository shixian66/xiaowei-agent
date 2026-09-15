"""渠道绑定与可靠投影订阅的存储契约和共享状态判定。"""

import datetime as dt
from typing import Protocol, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts import (
    AwareDatetime,
    ChannelKind,
    Contract,
    DestinationKind,
    ProjectionErrorCode,
    ProjectionState,
    Sha256Hex,
    StrictInt,
    StrictStr,
    TaskId,
    TaskLookup,
)


class ChannelStoreError(RuntimeError):
    """不把不透明渠道引用放进异常文本的存储错误基类。"""


class ChannelBindingNotFoundError(ChannelStoreError, LookupError):
    """指定 scope 内不存在群绑定。"""

    def __init__(self) -> None:
        super().__init__("channel binding not found")


class ChannelBindingConflictError(ChannelStoreError):
    """同一来源引用被绑定到了不同语义。"""

    def __init__(self) -> None:
        super().__init__("channel binding conflict")


class ProjectionSubscriptionNotFoundError(ChannelStoreError, LookupError):
    """投影订阅不存在。"""

    def __init__(self) -> None:
        super().__init__("projection subscription not found")


class ProjectionSubscriptionConflictError(ChannelStoreError):
    """同一任务与目的地被创建成不同语义的订阅。"""

    def __init__(self) -> None:
        super().__init__("projection subscription conflict")


class ProjectionClaimNotFoundError(ChannelStoreError, LookupError):
    """投影 claim 不存在、已过期、已失效或没有对应渠道绑定。"""

    def __init__(self) -> None:
        super().__init__("projection claim not found")


class ChannelBinding(Contract):
    """渠道来源与 Runtime 任务的一对一关联；不复制任务事实。"""

    binding_id: StrictStr
    task_id: TaskId
    tenant_id: StrictStr
    environment_id: StrictStr
    channel: ChannelKind
    initiator_subject_ref: StrictStr
    conversation_ref: StrictStr | None = None
    source_event_ref: StrictStr
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _group_has_a_conversation(self) -> Self:
        if self.channel is ChannelKind.FEISHU_GROUP and self.conversation_ref is None:
            raise ValueError("group binding requires conversation_ref")
        return self


class ProjectionSubscription(Contract):
    """异步渠道投影的持久化状态；任务状态始终回读 TaskStore。"""

    subscription_id: StrictStr
    task_id: TaskId
    destination_kind: DestinationKind
    destination_ref: StrictStr
    source_message_ref: StrictStr | None = None
    state: ProjectionState
    last_projected_task_version: StrictInt | None = Field(default=None, ge=0)
    attempt_number: StrictInt = Field(ge=0)
    claim_owner: StrictStr | None = None
    claim_expires_at: AwareDatetime | None = None
    fencing_token: StrictInt | None = Field(default=None, gt=0)
    next_attempt_at: AwareDatetime
    provider_failure_count: StrictInt = Field(ge=0)
    last_error_code: ProjectionErrorCode | None = None
    payload_digest: Sha256Hex | None = None

    @model_validator(mode="after")
    def _fields_are_consistent(self) -> Self:
        claimed = self.claim_owner is not None
        if claimed != (self.claim_expires_at is not None) or claimed != (
            self.fencing_token is not None
        ):
            raise ValueError("projection claim fields must be set or cleared together")
        if self.state in {ProjectionState.COMPLETED, ProjectionState.DEAD_LETTER} and claimed:
            raise ValueError("terminal projection cannot retain a claim")
        projected = self.last_projected_task_version is not None
        if projected != (self.payload_digest is not None):
            raise ValueError(
                "projection version and payload digest must be set or cleared together"
            )
        failed = self.provider_failure_count > 0
        if failed != (self.last_error_code is not None):
            raise ValueError("provider failure count and error code must agree")
        if self.state is ProjectionState.COMPLETED and (
            not projected or self.source_message_ref is None
        ):
            raise ValueError(
                "completed projection must record its message, task version and digest"
            )
        return self


class CreateProjectionSubscriptionCommand(Contract):
    task_id: TaskId
    destination_kind: DestinationKind
    destination_ref: StrictStr
    initial_state: ProjectionState
    next_attempt_at: AwareDatetime

    @model_validator(mode="after")
    def _initial_state_is_supported(self) -> Self:
        if self.initial_state not in {
            ProjectionState.PENDING_INITIAL,
            ProjectionState.WAITING_TERMINAL,
        }:
            raise ValueError("projection subscription must start pending or waiting")
        return self


class BindTaskCommand(Contract):
    task_id: TaskId
    tenant_id: StrictStr
    environment_id: StrictStr
    channel: ChannelKind
    initiator_subject_ref: StrictStr
    conversation_ref: StrictStr | None = None
    source_event_ref: StrictStr
    created_at: AwareDatetime
    projection: CreateProjectionSubscriptionCommand | None = None

    @model_validator(mode="after")
    def _projection_belongs_to_task(self) -> Self:
        if self.projection is not None and self.projection.task_id != self.task_id:
            raise ValueError("projection subscription belongs to a different task")
        if self.channel is ChannelKind.FEISHU_GROUP and self.conversation_ref is None:
            raise ValueError("group binding requires conversation_ref")
        return self


class GroupBindingLookup(Contract):
    task_id: TaskId
    tenant_id: StrictStr
    environment_id: StrictStr


class ChannelBindingLookup(Contract):
    """按任务与作用域读取唯一渠道绑定，不对渠道种类做推断。"""

    task_id: TaskId
    tenant_id: StrictStr
    environment_id: StrictStr


class GroupBoundTaskIdsQuery(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    task_ids: tuple[TaskId, ...] = Field(min_length=1, max_length=100)


class ProjectionDueQuery(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    limit: StrictInt = Field(gt=0, le=100)


class ClaimProjectionCommand(Contract):
    subscription_id: StrictStr
    claim_owner: StrictStr
    ttl_seconds: StrictInt = Field(gt=0)
    expected_state: ProjectionState


class ProjectionClaimMutation(Contract):
    subscription_id: StrictStr
    claim_owner: StrictStr
    fencing_token: StrictInt = Field(gt=0)
    expected_state: ProjectionState


class RenewProjectionClaimCommand(ProjectionClaimMutation):
    """在出站调用前为同一 live owner/token 刷新租约，不生成新 fence。"""

    ttl_seconds: StrictInt = Field(gt=0)


class ClaimedTaskLookup(Contract):
    """解析投影任务 scope 所需的最小 live-claim 凭据。"""

    subscription_id: StrictStr
    claim_owner: StrictStr
    fencing_token: StrictInt = Field(gt=0)


class RecordInitialProjectionCommand(ProjectionClaimMutation):
    source_message_ref: StrictStr
    task_version: StrictInt = Field(ge=0)
    payload_digest: Sha256Hex
    task_terminal: bool
    next_attempt_at: AwareDatetime


class ScheduleTaskRecheckCommand(ProjectionClaimMutation):
    next_attempt_at: AwareDatetime


class ScheduleProviderRetryCommand(ProjectionClaimMutation):
    next_attempt_at: AwareDatetime
    error_code: ProjectionErrorCode


class CompleteProjectionCommand(ProjectionClaimMutation):
    source_message_ref: StrictStr
    task_version: StrictInt = Field(ge=0)
    payload_digest: Sha256Hex


class DeadLetterProjectionCommand(ProjectionClaimMutation):
    error_code: ProjectionErrorCode


class ProjectionUpdateResult(Contract):
    applied: bool
    winner: ProjectionSubscription


def binding_matches_command(binding: ChannelBinding, command: BindTaskCommand) -> bool:
    """忽略重试时间戳，只比较来源绑定的稳定语义。"""
    return (
        binding.task_id == command.task_id
        and binding.tenant_id == command.tenant_id
        and binding.environment_id == command.environment_id
        and binding.channel is command.channel
        and binding.initiator_subject_ref == command.initiator_subject_ref
        and binding.conversation_ref == command.conversation_ref
        and binding.source_event_ref == command.source_event_ref
    )


def subscription_matches_command(
    subscription: ProjectionSubscription,
    command: CreateProjectionSubscriptionCommand,
) -> bool:
    """忽略首次调度时刻；未投递时核对初态，已推进时以持久化 winner 为准。

    初态不是独立持久化字段。订阅一旦被 claim 或投递，拿新的创建命令重置/重开它比
    接受既有 winner 更危险；目的地身份仍必须逐项完全相同。
    """
    identity_matches = (
        subscription.task_id == command.task_id
        and subscription.destination_kind is command.destination_kind
        and subscription.destination_ref == command.destination_ref
    )
    untouched = (
        subscription.attempt_number == 0
        and subscription.last_projected_task_version is None
        and subscription.provider_failure_count == 0
    )
    return identity_matches and (
        not untouched or subscription.state is command.initial_state
    )


def claim_projection(
    current: ProjectionSubscription,
    command: ClaimProjectionCommand,
    *,
    now: dt.datetime,
    fencing_token: int,
) -> ProjectionUpdateResult:
    """在纯函数中裁决 due、live claim、状态与 fencing。"""
    live_claim = (
        current.claim_owner is not None
        and current.claim_expires_at is not None
        and current.claim_expires_at > now
    )
    terminal = current.state in {
        ProjectionState.COMPLETED,
        ProjectionState.DEAD_LETTER,
    }
    if (
        terminal
        or current.state is not command.expected_state
        or current.next_attempt_at > now
        or live_claim
    ):
        return ProjectionUpdateResult(applied=False, winner=current)
    state = (
        ProjectionState.DELIVERING_TERMINAL
        if current.state is ProjectionState.WAITING_TERMINAL
        else current.state
    )
    winner = current.model_copy(
        update={
            "state": state,
            "attempt_number": current.attempt_number + 1,
            "claim_owner": command.claim_owner,
            "claim_expires_at": now + dt.timedelta(seconds=command.ttl_seconds),
            "fencing_token": fencing_token,
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


def claim_matches(
    current: ProjectionSubscription,
    command: ProjectionClaimMutation,
    *,
    now: dt.datetime,
) -> bool:
    """更新只能由仍持有 live claim 的同 owner/token 在预期状态提交。"""
    return (
        current.state is command.expected_state
        and current.claim_owner == command.claim_owner
        and current.fencing_token == command.fencing_token
        and current.claim_expires_at is not None
        and current.claim_expires_at > now
    )


def renew_projection_claim(
    current: ProjectionSubscription,
    command: RenewProjectionClaimCommand,
    *,
    now: dt.datetime,
) -> ProjectionUpdateResult:
    """只允许当前 live claim 延长自身租约；不得缩短或复活租约。"""
    if (
        not claim_matches(current, command, now=now)
        or current.claim_expires_at is None
    ):
        return ProjectionUpdateResult(applied=False, winner=current)
    requested_expiry = now + dt.timedelta(seconds=command.ttl_seconds)
    winner = current.model_copy(
        update={
            "claim_expires_at": max(current.claim_expires_at, requested_expiry),
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


def authorize_claimed_task_lookup(
    subscription: ProjectionSubscription,
    binding: ChannelBinding | None,
    lookup: ClaimedTaskLookup,
    *,
    now: dt.datetime,
) -> TaskLookup:
    """仅用仍有效的 owner/token 解析任务 scope；所有失败形态统一隐藏。"""
    if (
        subscription.subscription_id != lookup.subscription_id
        or subscription.claim_owner != lookup.claim_owner
        or subscription.fencing_token != lookup.fencing_token
        or subscription.claim_expires_at is None
        or subscription.claim_expires_at <= now
        or binding is None
        or binding.task_id != subscription.task_id
    ):
        raise ProjectionClaimNotFoundError
    return TaskLookup(
        task_id=binding.task_id,
        tenant_id=binding.tenant_id,
        environment_id=binding.environment_id,
    )


def record_initial_projection(
    current: ProjectionSubscription,
    command: RecordInitialProjectionCommand,
    *,
    now: dt.datetime,
) -> ProjectionUpdateResult:
    if (
        current.state is not ProjectionState.PENDING_INITIAL
        or not claim_matches(current, command, now=now)
    ):
        return ProjectionUpdateResult(applied=False, winner=current)
    state = (
        ProjectionState.COMPLETED
        if command.task_terminal
        else ProjectionState.WAITING_TERMINAL
    )
    winner = current.model_copy(
        update={
            "state": state,
            "source_message_ref": command.source_message_ref,
            "last_projected_task_version": command.task_version,
            "payload_digest": command.payload_digest,
            "next_attempt_at": command.next_attempt_at,
            "claim_owner": None,
            "claim_expires_at": None,
            "fencing_token": None,
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


def schedule_task_recheck(
    current: ProjectionSubscription,
    command: ScheduleTaskRecheckCommand,
    *,
    now: dt.datetime,
) -> ProjectionUpdateResult:
    if (
        current.state is not ProjectionState.DELIVERING_TERMINAL
        or not claim_matches(current, command, now=now)
    ):
        return ProjectionUpdateResult(applied=False, winner=current)
    winner = current.model_copy(
        update={
            "state": ProjectionState.WAITING_TERMINAL,
            "next_attempt_at": command.next_attempt_at,
            "claim_owner": None,
            "claim_expires_at": None,
            "fencing_token": None,
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


def schedule_provider_retry(
    current: ProjectionSubscription,
    command: ScheduleProviderRetryCommand,
    *,
    now: dt.datetime,
) -> ProjectionUpdateResult:
    if current.state not in {
        ProjectionState.PENDING_INITIAL,
        ProjectionState.DELIVERING_TERMINAL,
    } or not claim_matches(current, command, now=now):
        return ProjectionUpdateResult(applied=False, winner=current)
    winner = current.model_copy(
        update={
            "next_attempt_at": command.next_attempt_at,
            "provider_failure_count": current.provider_failure_count + 1,
            "last_error_code": command.error_code,
            "claim_owner": None,
            "claim_expires_at": None,
            "fencing_token": None,
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


def complete_projection(
    current: ProjectionSubscription,
    command: CompleteProjectionCommand,
    *,
    now: dt.datetime,
) -> ProjectionUpdateResult:
    if current.state not in {
        ProjectionState.PENDING_INITIAL,
        ProjectionState.DELIVERING_TERMINAL,
    } or not claim_matches(current, command, now=now):
        return ProjectionUpdateResult(applied=False, winner=current)
    winner = current.model_copy(
        update={
            "state": ProjectionState.COMPLETED,
            "source_message_ref": command.source_message_ref,
            "last_projected_task_version": command.task_version,
            "payload_digest": command.payload_digest,
            "claim_owner": None,
            "claim_expires_at": None,
            "fencing_token": None,
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


def dead_letter_projection(
    current: ProjectionSubscription,
    command: DeadLetterProjectionCommand,
    *,
    now: dt.datetime,
) -> ProjectionUpdateResult:
    if current.state not in {
        ProjectionState.PENDING_INITIAL,
        ProjectionState.DELIVERING_TERMINAL,
    } or not claim_matches(current, command, now=now):
        return ProjectionUpdateResult(applied=False, winner=current)
    winner = current.model_copy(
        update={
            "state": ProjectionState.DEAD_LETTER,
            "provider_failure_count": current.provider_failure_count + 1,
            "last_error_code": command.error_code,
            "claim_owner": None,
            "claim_expires_at": None,
            "fencing_token": None,
        }
    )
    return ProjectionUpdateResult(applied=True, winner=winner)


class ChannelStore(Protocol):
    """渠道绑定与可靠投影订阅的唯一持久化端口。"""

    async def bind_task(self, *, command: BindTaskCommand) -> ChannelBinding:
        """幂等绑定来源；嵌套 projection 必须在同一事务内创建或恢复。"""

    async def get_group_binding(self, *, lookup: GroupBindingLookup) -> ChannelBinding:
        """按 task 与 scope 读取群绑定；其余情形统一未找到。"""

    async def get_binding(self, *, lookup: ChannelBindingLookup) -> ChannelBinding:
        """按 task 与 scope 读取唯一绑定；不存在或错 scope 统一未找到。"""

    async def list_group_bound_task_ids(
        self, *, query: GroupBoundTaskIdsQuery
    ) -> frozenset[str]:
        """批量返回指定 scope 中存在飞书群绑定的任务 ID。"""

    async def create_projection_subscription(
        self, *, command: CreateProjectionSubscriptionCommand
    ) -> ProjectionSubscription:
        """按任务和目的地幂等创建；只有已有渠道绑定的订阅才进入 due 队列。"""

    async def list_due_projection_subscriptions(
        self, *, query: ProjectionDueQuery
    ) -> tuple[ProjectionSubscription, ...]:
        """稳定列出指定 scope 内到期且无 live claim 的订阅，不改变状态。"""

    async def claim_projection_subscription(
        self, *, command: ClaimProjectionCommand
    ) -> ProjectionUpdateResult:
        """竞争订阅并取得单调 fencing token。"""

    async def resolve_claimed_task_lookup(
        self, *, lookup: ClaimedTaskLookup
    ) -> TaskLookup:
        """仅为 live projection claim 解析 TaskStore 所需 scope。"""

    async def renew_projection_claim(
        self, *, command: RenewProjectionClaimCommand
    ) -> ProjectionUpdateResult:
        """紧邻出站前续期同一 live claim；不得复活过期 claim。"""

    async def record_initial_projection(
        self, *, command: RecordInitialProjectionCommand
    ) -> ProjectionUpdateResult: ...

    async def schedule_task_recheck(
        self, *, command: ScheduleTaskRecheckCommand
    ) -> ProjectionUpdateResult: ...

    async def schedule_provider_retry(
        self, *, command: ScheduleProviderRetryCommand
    ) -> ProjectionUpdateResult: ...

    async def complete_projection(
        self, *, command: CompleteProjectionCommand
    ) -> ProjectionUpdateResult: ...

    async def dead_letter_projection(
        self, *, command: DeadLetterProjectionCommand
    ) -> ProjectionUpdateResult: ...


__all__ = [
    "BindTaskCommand",
    "ChannelBinding",
    "ChannelBindingConflictError",
    "ChannelBindingLookup",
    "ChannelBindingNotFoundError",
    "ChannelStore",
    "ChannelStoreError",
    "ClaimProjectionCommand",
    "ClaimedTaskLookup",
    "CompleteProjectionCommand",
    "CreateProjectionSubscriptionCommand",
    "DeadLetterProjectionCommand",
    "GroupBindingLookup",
    "GroupBoundTaskIdsQuery",
    "ProjectionClaimNotFoundError",
    "ProjectionDueQuery",
    "ProjectionSubscription",
    "ProjectionSubscriptionConflictError",
    "ProjectionSubscriptionNotFoundError",
    "ProjectionUpdateResult",
    "RecordInitialProjectionCommand",
    "RenewProjectionClaimCommand",
    "ScheduleProviderRetryCommand",
    "ScheduleTaskRecheckCommand",
    "authorize_claimed_task_lookup",
    "renew_projection_claim",
]

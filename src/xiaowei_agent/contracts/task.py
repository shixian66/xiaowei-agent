"""任务生命周期契约。

状态迁移表是**闭集**：不在表中的迁移一律拒绝。终态的出边显式写成空集合，而不是
"表里没有这个键"——后者会让 ``ALLOWED_TRANSITIONS[status]`` 抛 ``KeyError`` 而不是
给出确定的拒绝。终态集合与迁移表由测试交叉校验，避免两处各写一份而悄悄漂移。
"""

from collections.abc import Mapping
from itertools import pairwise
from types import MappingProxyType
from typing import Final, Self
from urllib.parse import quote

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    Sha256Hex,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.enums import TaskStatus, TransitionRejection
from xiaowei_agent.contracts.render import RenderPayload
from xiaowei_agent.contracts.request import RequestContext, RequestEnvelope

TERMINAL_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELED,
        TaskStatus.INDETERMINATE,
    }
)


class TaskSubmission(Contract):
    """创建任务时不可变持久化的完整提交事实。"""

    envelope: RequestEnvelope
    context: RequestContext
    as_of: AwareDatetime
    parent_task_id: StrictStr | None = None


class TaskLookup(Contract):
    """任务查询的存储作用域；M5 不把 actor 当作读取 ACL。"""

    task_id: StrictStr
    tenant_id: StrictStr
    environment_id: StrictStr


class ActorTaskPageQuery(Contract):
    """普通用户按 actor 读取任务页；游标筛选 ``created_seq < before_created_seq``。"""

    tenant_id: StrictStr
    environment_id: StrictStr
    actor: StrictStr
    before_created_seq: StrictInt | None = Field(default=None, gt=0)
    limit: StrictInt = Field(gt=0, le=100)


class ScopeTaskPageQuery(Contract):
    """admin 按 scope 读取任务页；游标筛选 ``created_seq < before_created_seq``。"""

    tenant_id: StrictStr
    environment_id: StrictStr
    before_created_seq: StrictInt | None = Field(default=None, gt=0)
    limit: StrictInt = Field(gt=0, le=100)


def task_query_path(task_id: str) -> str:
    """生成任务查询的相对路径；保留字符不能改变路由层级。"""
    return f"/v1/tasks/{quote(task_id, safe='')}"


class TaskView(Contract):
    """入口层可见的任务投影；不暴露租约、版本或失败预算。"""

    task_id: StrictStr
    status: TaskStatus
    render: RenderPayload | None = None
    query_path: StrictStr

    @model_validator(mode="after")
    def _projection_is_consistent(self) -> Self:
        terminal = self.status in TERMINAL_STATUSES
        if terminal != (self.render is not None):
            raise ValueError("render presence must agree with terminal status")
        if self.render is not None and self.render.status is not self.status:
            raise ValueError("render status must agree with task status")
        if self.query_path != task_query_path(self.task_id):
            raise ValueError("query_path does not belong to task_id")
        return self

ALLOWED_TRANSITIONS: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        TaskStatus.CREATED: frozenset(
            {
                TaskStatus.PLANNING,
                TaskStatus.CANCELED,
                TaskStatus.FAILED,
                TaskStatus.REJECTED,
            }
        ),
        TaskStatus.PLANNING: frozenset(
            {
                TaskStatus.RUNNING,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.REJECTED,
            }
        ),
        TaskStatus.RUNNING: frozenset(
            {
                TaskStatus.AWAITING_APPROVAL,
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.INDETERMINATE,
                TaskStatus.REJECTED,
            }
        ),
        TaskStatus.AWAITING_APPROVAL: frozenset(
            {
                TaskStatus.RUNNING,
                TaskStatus.REJECTED,
                TaskStatus.CANCELED,
                TaskStatus.FAILED,
            }
        ),
        **{status: frozenset() for status in TERMINAL_STATUSES},
    }
)


class TaskRecord(Contract):
    task_id: StrictStr
    tenant_id: StrictStr
    environment_id: StrictStr
    actor: StrictStr
    idempotency_key: StrictStr
    request_digest: Sha256Hex
    status: TaskStatus
    version: StrictInt = Field(ge=0)
    created_seq: StrictInt = Field(gt=0)
    attempt_number: StrictInt = Field(ge=0)
    task_failure_count: StrictInt = Field(ge=0)
    next_attempt_at: AwareDatetime | None
    retry_scheduled_by_attempt: StrictInt | None = Field(default=None, gt=0)
    retry_command_digest: Sha256Hex | None = None
    lease_owner: StrictStr | None = None
    lease_expires_at: AwareDatetime | None = None
    fencing_token: StrictInt | None = Field(default=None, gt=0)
    terminal_reason: StrictStr | None = None

    @model_validator(mode="after")
    def _lease_fields_are_consistent(self) -> Self:
        """三个租约字段必须同时置位或同时清空。

        任一单独存在都意味着状态机中间态泄漏出来了；此时"是否持有租约"没有确定
        答案，fencing 检查也就失去依据。
        """
        held = self.lease_owner is not None
        if held != (self.lease_expires_at is not None) or held != (
            self.fencing_token is not None
        ):
            raise ValueError(
                "lease_owner, lease_expires_at and fencing_token must be set or cleared together"
            )
        return self

    @model_validator(mode="after")
    def _retry_marker_fields_are_consistent(self) -> Self:
        attempt = self.retry_scheduled_by_attempt
        marked = attempt is not None
        if marked != (self.retry_command_digest is not None):
            raise ValueError(
                "retry_scheduled_by_attempt and retry_command_digest "
                "must be set or cleared together"
            )
        if attempt is not None and attempt > self.attempt_number:
            raise ValueError("retry marker cannot refer to a future attempt")
        return self


class StoredTaskRead(Contract):
    """TaskStore 同次读取返回的任务事实与不可变提交。"""

    record: TaskRecord
    submission: TaskSubmission


class StoredTaskPage(Contract):
    """按 ``created_seq`` 游标分页的存储层读取结果。

    ``next_created_seq`` 为 ``None`` 表示没有下一页；若存在，则必须指向本页最后
    一条记录，确保它能原样成为下一次查询的排他游标。
    """

    items: tuple[StoredTaskRead, ...]
    next_created_seq: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _cursor_matches_page(self) -> Self:
        created_seqs = tuple(item.record.created_seq for item in self.items)
        if any(current <= following for current, following in pairwise(created_seqs)):
            raise ValueError("page items must be strictly descending by created_seq")
        if not self.items:
            if self.next_created_seq is not None:
                raise ValueError("empty page cannot have a cursor")
            return self
        if (
            self.next_created_seq is not None
            and self.next_created_seq != self.items[-1].record.created_seq
        ):
            raise ValueError("cursor must match the last item created_seq")
        return self


class LeaseGrant(Contract):
    task_id: StrictStr
    owner: StrictStr
    expires_at: AwareDatetime
    fencing_token: StrictInt = Field(gt=0)


class TransitionResult(Contract):
    applied: bool
    winner: TaskRecord
    rejection: TransitionRejection | None = None

    @model_validator(mode="after")
    def _applied_xor_rejection(self) -> Self:
        if self.applied is (self.rejection is not None):
            raise ValueError("applied and rejection must be mutually exclusive")
        return self


class TaskOutcome(Contract):
    task_id: StrictStr
    status: TaskStatus
    terminal_reason: StrictStr | None
    evidence_refs: tuple[StrictStr, ...]
    render_ref: StrictStr | None

    @model_validator(mode="after")
    def _status_must_be_terminal(self) -> Self:
        if self.status not in TERMINAL_STATUSES:
            raise ValueError(f"TaskOutcome requires a terminal status, got {self.status}")
        return self

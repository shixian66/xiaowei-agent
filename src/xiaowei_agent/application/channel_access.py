"""Web/飞书共用的任务读取授权与安全摘要投影。"""

import logging
from typing import Final, Protocol

from pydantic import Field

from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.contracts import (
    ActorTaskPageQuery,
    AuthenticatedPrincipal,
    AwareDatetime,
    ChannelPermission,
    Contract,
    NonEmptyText,
    ScopeTaskPageQuery,
    StoredTaskRead,
    StrictInt,
    StrictStr,
    TaskLookup,
    TaskRecord,
    TaskStatus,
    TaskView,
)
from xiaowei_agent.persistence.channel import (
    ChannelBindingNotFoundError,
    ChannelStore,
    GroupBindingLookup,
    GroupBoundTaskIdsQuery,
)
from xiaowei_agent.persistence.store import TaskNotFoundError, TaskStore
from xiaowei_agent.redaction import scrub_text

TASK_SUMMARY_PREVIEW_LIMIT: Final[int] = 240
TASK_DETAIL_PREVIEW_LIMIT: Final[int] = 8192
_LOGGER = logging.getLogger(__name__)


class TaskAccessNotFoundError(LookupError):
    """任务不存在或主体无权知道它是否存在。"""

    def __init__(self) -> None:
        super().__init__("task not found")


class TaskAccessSnapshotUnavailableError(RuntimeError):
    """任务持续变化，无法形成状态与版本一致的单次安全快照。"""

    def __init__(self) -> None:
        super().__init__("task snapshot unavailable")


class FeishuMembershipPort(Protocol):
    """实时群成员查询端口；实现位于后续飞书适配 PR。"""

    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        """当前主体仍在群内才返回真；异常由调用方 fail-closed。"""


class TaskAccessQuery(Contract):
    principal: AuthenticatedPrincipal
    task_id: StrictStr


class TaskListQuery(Contract):
    principal: AuthenticatedPrincipal
    before_created_seq: StrictInt | None = Field(default=None, gt=0)
    limit: StrictInt = Field(gt=0, le=100)


class AccessibleTask(Contract):
    task_view: TaskView
    request_preview: NonEmptyText = Field(max_length=TASK_DETAIL_PREVIEW_LIMIT)
    submitted_at: AwareDatetime
    task_version: StrictInt = Field(ge=0)


class TaskSummary(Contract):
    task_id: StrictStr
    status: TaskStatus
    request_preview: NonEmptyText = Field(max_length=TASK_SUMMARY_PREVIEW_LIMIT)
    submitted_at: AwareDatetime


class TaskPage(Contract):
    """安全任务摘要页；过滤群任务后允许空页携带向前推进的游标。"""

    items: tuple[TaskSummary, ...]
    next_created_seq: StrictInt | None = Field(default=None, gt=0)


def _preview(text: str, *, limit: int) -> str:
    return scrub_text(text)[:limit]


def _log_membership_check_failure(principal: AuthenticatedPrincipal) -> None:
    """只记录可归因的安全维度，不记录供应商正文或不透明身份引用。"""
    try:
        _LOGGER.warning(
            "feishu membership check failed",
            extra={
                "tenant_id": principal.tenant_id,
                "environment_id": principal.environment_id,
                "failure_kind": "membership_adapter_error",
            },
        )
    except Exception:
        # 诊断面故障不能把 fail-closed 的 404 变成另一种可区分结果。
        return


class TaskAccessService:
    """只做任务读取 ACL 与安全字段投影，不拥有执行或渲染规则。"""

    def __init__(
        self,
        *,
        runtime: TaskViewRuntime,
        task_store: TaskStore,
        channel_store: ChannelStore,
        membership: FeishuMembershipPort,
    ) -> None:
        self._runtime = runtime
        self._tasks = task_store
        self._channels = channel_store
        self._membership = membership

    @staticmethod
    def _lookup(principal: AuthenticatedPrincipal, task_id: str) -> TaskLookup:
        return TaskLookup(
            task_id=task_id,
            tenant_id=principal.tenant_id,
            environment_id=principal.environment_id,
        )

    async def _authorized_record(self, query: TaskAccessQuery) -> TaskRecord | None:
        principal = query.principal
        if ChannelPermission.VIEW_SAFE_TASK not in principal.permissions:
            return None
        lookup = self._lookup(principal, query.task_id)
        try:
            record = await self._tasks.get(lookup=lookup)
        except TaskNotFoundError:
            return None
        if (
            ChannelPermission.ADMIN_ALL_SAFE_TASKS in principal.permissions
            or record.actor == principal.actor
        ):
            return record
        try:
            binding = await self._channels.get_group_binding(
                lookup=GroupBindingLookup(
                    task_id=query.task_id,
                    tenant_id=principal.tenant_id,
                    environment_id=principal.environment_id,
                )
            )
        except ChannelBindingNotFoundError:
            return None
        if binding.conversation_ref is None:
            return None
        try:
            is_member = await self._membership.is_current_group_member(
                tenant_id=principal.tenant_id,
                conversation_ref=binding.conversation_ref,
                subject_ref=principal.subject_ref,
            )
        except Exception:
            _log_membership_check_failure(principal)
            return None
        return record if is_member else None

    async def get_task(self, *, query: TaskAccessQuery) -> AccessibleTask:
        """返回同一 Runtime 的安全任务详情；所有拒绝统一为未找到。"""
        record = await self._authorized_record(query)
        if record is None:
            raise TaskAccessNotFoundError
        lookup = self._lookup(query.principal, query.task_id)
        try:
            task_view: TaskView | None = None
            for _ in range(3):
                candidate = await self._runtime.project_task(record=record)
                after = await self._tasks.get(lookup=lookup)
                if record.version == after.version and candidate.status is after.status:
                    task_view = candidate
                    record = after
                    break
                record = after
            if task_view is None:
                raise TaskAccessSnapshotUnavailableError
            submission = await self._tasks.get_submission(lookup=lookup)
        except TaskNotFoundError:
            raise TaskAccessNotFoundError from None
        return AccessibleTask(
            task_view=task_view,
            request_preview=_preview(
                submission.envelope.text, limit=TASK_DETAIL_PREVIEW_LIMIT
            ),
            submitted_at=submission.as_of,
            task_version=record.version,
        )

    @staticmethod
    def _summary(item: StoredTaskRead) -> TaskSummary:
        return TaskSummary(
            task_id=item.record.task_id,
            status=item.record.status,
            request_preview=_preview(
                item.submission.envelope.text, limit=TASK_SUMMARY_PREVIEW_LIMIT
            ),
            submitted_at=item.submission.as_of,
        )

    async def list_tasks(self, *, query: TaskListQuery) -> TaskPage:
        """普通用户只看自己的非群任务；admin 看当前 scope 的全部安全摘要。"""
        principal = query.principal
        if ChannelPermission.VIEW_SAFE_TASK not in principal.permissions:
            raise TaskAccessNotFoundError
        if ChannelPermission.ADMIN_ALL_SAFE_TASKS in principal.permissions:
            stored = await self._tasks.list_tasks_for_scope(
                query=ScopeTaskPageQuery(
                    tenant_id=principal.tenant_id,
                    environment_id=principal.environment_id,
                    before_created_seq=query.before_created_seq,
                    limit=query.limit,
                )
            )
            return TaskPage(
                items=tuple(self._summary(item) for item in stored.items),
                next_created_seq=stored.next_created_seq,
            )

        stored = await self._tasks.list_tasks_for_actor(
            query=ActorTaskPageQuery(
                tenant_id=principal.tenant_id,
                environment_id=principal.environment_id,
                actor=principal.actor,
                before_created_seq=query.before_created_seq,
                limit=100,
            )
        )
        group_task_ids = (
            await self._channels.list_group_bound_task_ids(
                query=GroupBoundTaskIdsQuery(
                    tenant_id=principal.tenant_id,
                    environment_id=principal.environment_id,
                    task_ids=tuple(item.record.task_id for item in stored.items),
                )
            )
            if stored.items
            else frozenset()
        )
        summaries: list[TaskSummary] = []
        processed_created_seq: int | None = None
        has_more = stored.next_created_seq is not None
        for index, item in enumerate(stored.items):
            processed_created_seq = item.record.created_seq
            if item.record.task_id not in group_task_ids:
                summaries.append(self._summary(item))
            if len(summaries) == query.limit:
                has_more = has_more or index < len(stored.items) - 1
                break
        return TaskPage(
            items=tuple(summaries),
            next_created_seq=processed_created_seq if has_more else None,
        )


__all__ = [
    "TASK_DETAIL_PREVIEW_LIMIT",
    "TASK_SUMMARY_PREVIEW_LIMIT",
    "AccessibleTask",
    "FeishuMembershipPort",
    "TaskAccessNotFoundError",
    "TaskAccessQuery",
    "TaskAccessService",
    "TaskAccessSnapshotUnavailableError",
    "TaskListQuery",
    "TaskPage",
    "TaskSummary",
]

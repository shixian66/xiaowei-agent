"""从显式 Web 父任务链组装有界、脱敏的模型历史。"""

from dataclasses import dataclass
from typing import Final, Protocol, TypedDict

from xiaowei_agent.contracts import (
    MAX_MODEL_HISTORY_CHARACTERS,
    MAX_MODEL_HISTORY_ITEMS,
    MAX_MODEL_TEXT_CHARACTERS,
    TERMINAL_STATUSES,
    Channel,
    ChannelKind,
    RenderPayload,
    TaskLookup,
    TaskRecord,
    TaskSubmission,
)
from xiaowei_agent.persistence.channel import (
    ChannelBindingLookup,
    ChannelBindingNotFoundError,
    ChannelStore,
)
from xiaowei_agent.persistence.plans import PlanNotFoundError
from xiaowei_agent.persistence.store import TaskNotFoundError, TaskStore
from xiaowei_agent.planning import canonical_json
from xiaowei_agent.redaction import scrub_text

_SAFE_ROUND_KEYS: Final[frozenset[str]] = frozenset({"user", "assistant"})


class _SafeSection(TypedDict):
    title: str
    body: str


class _SafeAssistant(TypedDict):
    answer: str
    sections: tuple[_SafeSection, ...]
    next_steps: tuple[str, ...]
    status: str


class _SafeRound(TypedDict):
    user: str
    assistant: _SafeAssistant


class ParentContextRejectedError(PermissionError):
    """父链越权、损坏、循环或不满足终态约束。"""

    def __init__(self) -> None:
        super().__init__("parent context rejected")


class ParentContextUnavailableError(RuntimeError):
    """子任务已创建但渠道绑定尚未完成，可由 Worker 安全重试。"""

    def __init__(self) -> None:
        super().__init__("parent context temporarily unavailable")


class TerminalTaskProjector(Protocol):
    async def project_recorded(self, *, record: TaskRecord) -> RenderPayload:
        """从持久化事实重建既有安全终态投影。"""


@dataclass(frozen=True)
class AssembledContext:
    history: tuple[str, ...]
    truncated: bool


class ContextAssemblyPort(Protocol):
    async def assemble(
        self, *, task_id: str, submission: TaskSubmission
    ) -> AssembledContext:
        """重验显式父链并返回有界安全历史。"""


@dataclass(frozen=True)
class ParentTaskSnapshot:
    record: TaskRecord
    submission: TaskSubmission


async def load_web_parent_chain(
    *,
    task_store: TaskStore,
    channel_store: ChannelStore,
    parent_task_id: str,
    tenant_id: str,
    environment_id: str,
    actor: str,
    binding_owner: str,
    seen_task_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[ParentTaskSnapshot, ...], bool]:
    """按新到旧读取并复核父链；返回完整快照和是否因数量上限截断。"""
    seen = set(seen_task_ids)
    snapshots: list[ParentTaskSnapshot] = []
    truncated = False
    current_id: str | None = parent_task_id
    while current_id is not None:
        if current_id in seen:
            raise ParentContextRejectedError
        seen.add(current_id)
        lookup = TaskLookup(
            task_id=current_id,
            tenant_id=tenant_id,
            environment_id=environment_id,
        )
        try:
            record = await task_store.get(lookup=lookup)
            submission = await task_store.get_submission(lookup=lookup)
            binding = await channel_store.get_binding(
                lookup=ChannelBindingLookup(
                    task_id=current_id,
                    tenant_id=tenant_id,
                    environment_id=environment_id,
                )
            )
        except (TaskNotFoundError, ChannelBindingNotFoundError):
            raise ParentContextRejectedError from None
        if (
            record.status not in TERMINAL_STATUSES
            or record.actor != actor
            or submission.envelope.channel is not Channel.WEB
            or binding.channel is not ChannelKind.WEB
            or binding.initiator_subject_ref != binding_owner
        ):
            raise ParentContextRejectedError
        snapshots.append(ParentTaskSnapshot(record=record, submission=submission))
        current_id = submission.parent_task_id
        if current_id is not None and current_id in seen:
            raise ParentContextRejectedError
        if len(snapshots) == MAX_MODEL_HISTORY_ITEMS:
            truncated = current_id is not None
            break
    return tuple(snapshots), truncated


def _require_history_text(value: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_MODEL_TEXT_CHARACTERS:
        raise ParentContextRejectedError
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ParentContextRejectedError from None


def _round_payload(*, user_text: str, render: RenderPayload) -> _SafeRound:
    text_values = [user_text, render.answer, *render.next_steps]
    for section in render.sections:
        text_values.extend((section.title, section.body))
    for value in text_values:
        _require_history_text(value)
    return {
        "user": user_text,
        "assistant": {
            "answer": render.answer,
            "sections": tuple(
                {"title": section.title, "body": section.body}
                for section in render.sections
            ),
            "next_steps": render.next_steps,
            "status": render.status.value,
        },
    }


def _scrub_round(payload: _SafeRound) -> str:
    if payload.keys() != _SAFE_ROUND_KEYS:
        raise ParentContextRejectedError
    assistant = payload["assistant"]
    scrubbed = {
        "user": scrub_text(payload["user"]),
        "assistant": {
            "answer": scrub_text(assistant["answer"]),
            "sections": tuple(
                {
                    "title": scrub_text(section["title"]),
                    "body": scrub_text(section["body"]),
                }
                for section in assistant["sections"]
            ),
            "next_steps": tuple(
                scrub_text(item) for item in assistant["next_steps"]
            ),
            "status": assistant["status"],
        },
    }
    return canonical_json(scrubbed).decode("utf-8")


class ContextAssembler:
    """只沿持久化 parent 指针读取安全 Web 历史，不推断会话上下文。"""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        channel_store: ChannelStore,
        task_projector: TerminalTaskProjector,
    ) -> None:
        self._tasks = task_store
        self._channels = channel_store
        self._projector = task_projector

    @staticmethod
    def _lookup(submission: TaskSubmission, task_id: str) -> TaskLookup:
        return TaskLookup(
            task_id=task_id,
            tenant_id=submission.context.tenant_id,
            environment_id=submission.context.environment_id,
        )

    async def assemble(
        self, *, task_id: str, submission: TaskSubmission
    ) -> AssembledContext:
        """按时间正序返回完整历史轮次；临时绑定竞态与非法父链分开处理。"""
        parent_task_id = submission.parent_task_id
        if parent_task_id is None:
            return AssembledContext(history=(), truncated=False)
        if submission.envelope.channel is not Channel.WEB:
            raise ParentContextRejectedError

        current_lookup = self._lookup(submission, task_id)
        try:
            persisted = await self._tasks.get_submission(lookup=current_lookup)
        except TaskNotFoundError:
            raise ParentContextRejectedError from None
        if persisted != submission:
            raise ParentContextRejectedError
        try:
            current_binding = await self._channels.get_binding(
                lookup=ChannelBindingLookup(
                    task_id=task_id,
                    tenant_id=submission.context.tenant_id,
                    environment_id=submission.context.environment_id,
                )
            )
        except ChannelBindingNotFoundError:
            raise ParentContextUnavailableError from None
        if current_binding.channel is not ChannelKind.WEB:
            raise ParentContextRejectedError

        owner = current_binding.initiator_subject_ref
        actor = submission.context.actor
        newest_first: list[_SafeRound] = []
        raw_character_count = 0
        snapshots, truncated = await load_web_parent_chain(
            task_store=self._tasks,
            channel_store=self._channels,
            parent_task_id=parent_task_id,
            tenant_id=submission.context.tenant_id,
            environment_id=submission.context.environment_id,
            actor=actor,
            binding_owner=owner,
            seen_task_ids=frozenset({task_id}),
        )

        for snapshot in snapshots:
            try:
                render = await self._projector.project_recorded(
                    record=snapshot.record
                )
            except PlanNotFoundError:
                raise ParentContextRejectedError from None
            payload = _round_payload(
                user_text=snapshot.submission.envelope.text,
                render=render,
            )
            raw_round = canonical_json(payload).decode("utf-8")
            if (
                len(raw_round) > MAX_MODEL_TEXT_CHARACTERS
                or raw_character_count + len(raw_round)
                > MAX_MODEL_HISTORY_CHARACTERS
            ):
                truncated = True
                break
            newest_first.append(payload)
            raw_character_count += len(raw_round)

        history = tuple(_scrub_round(item) for item in reversed(newest_first))
        return AssembledContext(history=history, truncated=truncated)


__all__ = [
    "AssembledContext",
    "ContextAssembler",
    "ContextAssemblyPort",
    "ParentContextRejectedError",
    "ParentContextUnavailableError",
    "ParentTaskSnapshot",
    "TerminalTaskProjector",
    "load_web_parent_chain",
]

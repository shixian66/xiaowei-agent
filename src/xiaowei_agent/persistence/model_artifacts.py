"""Grant-fenced, insert-once 模型事实存储契约与内存实现。"""

from __future__ import annotations

from typing import Final, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from xiaowei_agent.contracts import (
    AwareDatetime,
    Contract,
    GrantRejection,
    IntentDraft,
    IntentSource,
    ModelAdvisory,
    ModelUsage,
    Sha256Hex,
    StrictInt,
    StrictStr,
    TaskId,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.persistence.decisions import grant_is_current
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import Clock, TaskAttemptGrant, TaskIdCarryingError

MODEL_ARTIFACT_VERSION: Literal[1] = 1


class ModelArtifactError(TaskIdCarryingError):
    """模型事实存储错误；task id 仅在结构化属性中。"""


class ModelArtifactConflictError(ModelArtifactError):
    """同一 task 已有不同的 insert-once 模型事实。"""


class ModelArtifactGrantError(ModelArtifactError):
    """保存者不再持有当前 task grant。"""


class ModelArtifactStateError(ModelArtifactError):
    """任务不存在、已终态或当前状态不允许写入该类模型事实。"""


_GRANT_LOSS_REJECTIONS: Final[frozenset[GrantRejection]] = frozenset(
    {GrantRejection.LEASE_NOT_HELD, GrantRejection.STALE_FENCING}
)


def require_model_artifact_grant(
    current: TaskRecord | None,
    grant: TaskAttemptGrant,
    *,
    now: AwareDatetime,
    allowed_statuses: frozenset[TaskStatus],
) -> None:
    """共享内存/PostgreSQL 的模型事实写入拒绝分类。"""
    if current is None:
        raise ModelArtifactStateError(
            "model artifact task does not exist", task_id=grant.task_id
        )
    rejection = grant_is_current(
        current,
        grant,
        now=now,
        allowed_statuses=allowed_statuses,
    )
    if rejection is None:
        return
    if rejection in _GRANT_LOSS_REJECTIONS:
        raise ModelArtifactGrantError(
            "model artifact grant is not current", task_id=grant.task_id
        )
    raise ModelArtifactStateError(
        "task status does not allow this model artifact", task_id=grant.task_id
    )


class IntentArtifactCandidate(Contract):
    """应用层已复验、尚未由存储层盖时间与 fencing 的 intent。"""

    draft: IntentDraft
    origin: Literal["model", "rule"]
    provider: StrictStr | None
    model: StrictStr | None
    provider_origin: StrictStr | None
    prompt_revision: StrictStr
    schema_revision: StrictStr
    input_digest: Sha256Hex
    result_digest: Sha256Hex
    usage: ModelUsage

    @model_validator(mode="after")
    def _identity_matches_origin(self) -> Self:
        identity = (self.provider, self.model, self.provider_origin)
        if self.origin == "model":
            if self.draft.source is not IntentSource.MODEL or any(
                value is None for value in identity
            ):
                raise ValueError("model intent requires complete provider identity")
        elif self.draft.source is IntentSource.MODEL or any(
            value is not None for value in identity
        ):
            raise ValueError("rule intent cannot carry provider identity")
        return self


class AdvisoryArtifactCandidate(Contract):
    """应用层已复验、尚未由存储层盖时间与 fencing 的 advisory。"""

    advisory: ModelAdvisory
    origin: Literal["model"] = "model"
    provider: StrictStr
    model: StrictStr
    provider_origin: StrictStr
    prompt_revision: StrictStr
    schema_revision: StrictStr
    input_digest: Sha256Hex
    result_digest: Sha256Hex
    usage: ModelUsage


class AcceptedIntentArtifact(IntentArtifactCandidate):
    artifact_version: Literal[1] = MODEL_ARTIFACT_VERSION
    task_id: TaskId
    created_at: AwareDatetime
    fencing_token: StrictInt = Field(gt=0)


class StoredModelAdvisory(AdvisoryArtifactCandidate):
    artifact_version: Literal[1] = MODEL_ARTIFACT_VERSION
    task_id: TaskId
    created_at: AwareDatetime
    fencing_token: StrictInt = Field(gt=0)


class ModelArtifactStore(Protocol):
    async def load_intent(self, *, task_id: str) -> AcceptedIntentArtifact | None: ...

    async def save_intent(
        self, *, grant: TaskAttemptGrant, candidate: IntentArtifactCandidate
    ) -> AcceptedIntentArtifact: ...

    async def load_advisory(self, *, task_id: str) -> StoredModelAdvisory | None: ...

    async def save_advisory(
        self, *, grant: TaskAttemptGrant, candidate: AdvisoryArtifactCandidate
    ) -> StoredModelAdvisory: ...


def artifact_matches_candidate(
    stored: AcceptedIntentArtifact | StoredModelAdvisory,
    candidate: IntentArtifactCandidate | AdvisoryArtifactCandidate,
) -> bool:
    return all(
        getattr(stored, name) == getattr(candidate, name)
        for name in type(candidate).model_fields
    )


class InMemoryModelArtifactStore:
    """与 TaskStore 共用锁和任务事实的单进程实现。"""

    def __init__(self, *, state: InMemoryPersistenceState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    async def load_intent(self, *, task_id: str) -> AcceptedIntentArtifact | None:
        async with self._state.lock:
            return cast(
                AcceptedIntentArtifact | None,
                self._state.accepted_intents.get(task_id),
            )

    async def save_intent(
        self, *, grant: TaskAttemptGrant, candidate: IntentArtifactCandidate
    ) -> AcceptedIntentArtifact:
        async with self._state.lock:
            current = self._state.tasks.get(grant.task_id)
            require_model_artifact_grant(
                current,
                grant,
                now=self._clock(),
                allowed_statuses=frozenset(
                    {TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING}
                ),
            )
            existing = self._state.accepted_intents.get(grant.task_id)
            if existing is not None:
                stored = cast(AcceptedIntentArtifact, existing)
                if not artifact_matches_candidate(stored, candidate):
                    raise ModelArtifactConflictError(
                        "different accepted intent is already stored",
                        task_id=grant.task_id,
                    )
                return stored
            stored = AcceptedIntentArtifact(
                **candidate.model_dump(mode="python"),
                task_id=grant.task_id,
                created_at=self._clock(),
                fencing_token=grant.fencing_token,
            )
            self._state.accepted_intents[grant.task_id] = stored
            return stored

    async def load_advisory(self, *, task_id: str) -> StoredModelAdvisory | None:
        async with self._state.lock:
            return cast(
                StoredModelAdvisory | None,
                self._state.model_advisories.get(task_id),
            )

    async def save_advisory(
        self, *, grant: TaskAttemptGrant, candidate: AdvisoryArtifactCandidate
    ) -> StoredModelAdvisory:
        async with self._state.lock:
            current = self._state.tasks.get(grant.task_id)
            require_model_artifact_grant(
                current,
                grant,
                now=self._clock(),
                allowed_statuses=frozenset({TaskStatus.RUNNING}),
            )
            existing = self._state.model_advisories.get(grant.task_id)
            if existing is not None:
                stored = cast(StoredModelAdvisory, existing)
                if not artifact_matches_candidate(stored, candidate):
                    raise ModelArtifactConflictError(
                        "different model advisory is already stored",
                        task_id=grant.task_id,
                    )
                return stored
            stored = StoredModelAdvisory(
                **candidate.model_dump(mode="python"),
                task_id=grant.task_id,
                created_at=self._clock(),
                fencing_token=grant.fencing_token,
            )
            self._state.model_advisories[grant.task_id] = stored
            return stored


__all__ = [
    "MODEL_ARTIFACT_VERSION",
    "AcceptedIntentArtifact",
    "AdvisoryArtifactCandidate",
    "InMemoryModelArtifactStore",
    "IntentArtifactCandidate",
    "ModelArtifactConflictError",
    "ModelArtifactGrantError",
    "ModelArtifactStateError",
    "ModelArtifactStore",
    "StoredModelAdvisory",
    "artifact_matches_candidate",
    "require_model_artifact_grant",
]

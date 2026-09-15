"""Durable execute path 的 accepted-interaction 恢复边界。"""

from typing import Any

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.model_interaction import (
    build_interaction_classifier_request,
    interaction_input_digest,
    interaction_result_digest,
)
from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.contracts import (
    AttemptIntent,
    IntentDraft,
    IntentSource,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelErrorCode,
    ModelInvocationProfile,
    ModelUsage,
    TaskStatus,
)
from xiaowei_agent.persistence.model_artifacts import InteractionArtifactCandidate
from xiaowei_agent.persistence.store import TaskAttemptCommand


class _InteractionPort:
    def __init__(self, result: InteractionModelResult | ModelErrorCode) -> None:
        self.result = result
        self.calls = 0
        self.requests: list[Any] = []

    async def classify(self, request: Any) -> InteractionModelResult:
        self.requests.append(request)
        self.calls += 1
        if isinstance(self.result, ModelErrorCode):
            raise ModelPortError(self.result)
        return self.result


def _capability(**slots: str) -> IntentDraft:
    return IntentDraft(
        intent="starrocks.slow_query.diagnose",
        slots={"window_minutes": "30"} | slots,
        missing=(),
        confidence=0.8,
        source=IntentSource.MODEL,
    )


def _result(
    kind: InteractionKind = InteractionKind.CAPABILITY_REQUEST,
    *,
    capability: IntentDraft | None = None,
) -> InteractionModelResult:
    return InteractionModelResult(
        draft=InteractionDraft(
            proposed_kind=kind,
            capability_draft=(
                _capability() if kind is InteractionKind.CAPABILITY_REQUEST else capability
            ),
            confidence=0.8,
            source=InteractionSource.MODEL,
        ),
        usage=ModelUsage(input_tokens=9, output_tokens=4),
    )


async def _durable_attempt(harness: RuntimeHarness, text: str) -> tuple[Any, Any]:
    submission = harness.submission(text)
    view = await harness.runtime.submit_task(submission=submission)
    harness.task_id = view.task_id
    attempted = await harness.store.begin_task_attempt(
        command=TaskAttemptCommand(
            task_id=view.task_id,
            intent=AttemptIntent.DISPATCH,
            owner="worker-1",
            ttl_seconds=60,
            trace_id=submission.context.trace_id,
        )
    )
    assert attempted.grant is not None and attempted.submission is not None
    return attempted.grant, attempted.submission


@pytest.mark.asyncio
async def test_runtime_persists_model_interaction_before_resolver() -> None:
    port = _InteractionPort(_result())
    harness = RuntimeHarness(GOLDEN, interaction_classifier=port)

    grant, submission = await _durable_attempt(harness, "最近30分钟有哪些慢查询")
    outcome = await harness.runtime.execute_task(grant=grant, submission=submission)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert port.calls == 1
    artifact = await harness.model_artifacts.load_interaction(
        task_id=outcome.task_id
    )
    assert artifact is not None
    assert artifact.draft == port.result.draft
    assert artifact.origin == "model"


@pytest.mark.asyncio
async def test_runtime_reuses_matching_interaction_artifact_without_model_call() -> None:
    port = _InteractionPort(ModelErrorCode.UNAVAILABLE)
    harness = RuntimeHarness(GOLDEN, interaction_classifier=port)
    grant, submission = await _durable_attempt(harness, "最近30分钟有哪些慢查询")
    profile = ModelInvocationProfile()
    request = build_interaction_classifier_request(user_text=submission.envelope.text)
    draft = _result().draft
    saved = await harness.model_artifacts.save_interaction(
        grant=grant,
        candidate=InteractionArtifactCandidate(
            draft=draft,
            origin="model",
            provider=profile.provider,
            model=profile.model,
            provider_origin=profile.origin,
            prompt_revision=profile.interaction_prompt_revision,
            schema_revision=profile.interaction_schema_revision,
            input_digest=interaction_input_digest(request, profile=profile),
            result_digest=interaction_result_digest(draft),
            usage=ModelUsage(input_tokens=9, output_tokens=4),
        ),
    )

    outcome = await harness.runtime.execute_task(grant=grant, submission=submission)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert port.calls == 0
    assert await harness.model_artifacts.load_interaction(task_id=grant.task_id) == saved


@pytest.mark.asyncio
async def test_retryable_provider_error_falls_back_to_rule_interaction_once() -> None:
    port = _InteractionPort(ModelErrorCode.RATE_LIMITED)
    harness = RuntimeHarness(GOLDEN, interaction_classifier=port)

    grant, submission = await _durable_attempt(harness, "最近30分钟有哪些慢查询")
    outcome = await harness.runtime.execute_task(grant=grant, submission=submission)

    assert outcome.status is TaskStatus.SUCCEEDED
    assert port.calls == 1
    artifact = await harness.model_artifacts.load_interaction(task_id=grant.task_id)
    assert artifact is not None
    assert artifact.origin == "rule"
    assert artifact.draft.proposed_kind is InteractionKind.CAPABILITY_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft",
    [
        InteractionDraft(
            proposed_kind=InteractionKind.CONVERSATION,
            capability_draft=None,
            confidence=0.8,
            source=InteractionSource.MODEL,
        ),
        InteractionDraft(
            proposed_kind=InteractionKind.CAPABILITY_REQUEST,
            capability_draft=_capability(environment_id="prod"),
            confidence=0.8,
            source=InteractionSource.MODEL,
        ),
    ],
)
async def test_router_rejection_stops_before_gateway(draft: InteractionDraft) -> None:
    port = _InteractionPort(InteractionModelResult(draft=draft, usage=ModelUsage()))
    harness = RuntimeHarness(GOLDEN, interaction_classifier=port)

    grant, submission = await _durable_attempt(harness, "最近30分钟有哪些慢查询")
    outcome = await harness.runtime.execute_task(grant=grant, submission=submission)

    assert outcome.status is TaskStatus.REJECTED
    assert port.calls == 1
    assert harness.calls == []

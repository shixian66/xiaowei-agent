"""无执行权任务投影 Runtime 的共享行为契约。"""

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application import task_view_runtime as task_view_module
from xiaowei_agent.application.capability_runtime import CapabilityRuntimeBinding
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.contracts import (
    AnswerabilityVerdict,
    EvidenceEnvelope,
    RenderPayload,
    TaskRecord,
    TaskStatus,
)


def _task_views(harness: RuntimeHarness) -> TaskViewRuntime:
    return TaskViewRuntime(
        task_store=harness.store,
        plan_store=harness.plan_store,
        ledger=harness.ledger,
        bindings=harness.runtime._bindings,
    )


async def test_full_and_narrow_runtimes_share_the_terminal_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    narrow = _task_views(harness)
    original = task_view_module.project_terminal
    calls = 0

    def counting_projection(
        *,
        record: TaskRecord,
        evidences: tuple[EvidenceEnvelope, ...],
        verdict: AnswerabilityVerdict,
        binding: CapabilityRuntimeBinding,
    ) -> RenderPayload:
        nonlocal calls
        calls += 1
        return original(
            record=record,
            evidences=evidences,
            verdict=verdict,
            binding=binding,
        )

    monkeypatch.setattr(task_view_module, "project_terminal", counting_projection)

    full_view = await harness.runtime.query_task(lookup=harness.lookup)
    narrow_view = await narrow.query_task(lookup=harness.lookup)

    assert full_view == narrow_view
    assert calls == 2


async def test_narrow_runtime_submit_only_persists_a_task() -> None:
    harness = RuntimeHarness(GOLDEN)
    narrow = _task_views(harness)

    view = await narrow.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )

    assert view.status is TaskStatus.CREATED
    assert view.render is None
    assert harness.gateway.invocations == 0
    assert await harness.store.load_step_executions(task_id=view.task_id) == ()


async def test_narrow_runtime_replays_an_existing_terminal_submission_without_execution(
) -> None:
    harness = RuntimeHarness(GOLDEN)
    rendered = await harness.handle("最近30分钟有哪些慢查询")
    invocations = harness.gateway.invocations
    narrow = _task_views(harness)

    replayed = await narrow.submit_task(
        submission=harness.submission("最近30分钟有哪些慢查询")
    )

    assert replayed.status is TaskStatus.SUCCEEDED
    assert replayed.render == rendered
    assert harness.gateway.invocations == invocations

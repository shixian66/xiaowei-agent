"""LocalStack 装配根不依赖 tests/ 语料。"""

import asyncio

import pytest
from tests.fakes.clock import ManualClock

from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    ReadinessReport,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces.local_stack import (
    LocalStack,
    build_in_memory_local_stack,
)
from xiaowei_agent.tools.gateway import DeterministicToolGateway


@pytest.mark.asyncio
async def test_in_memory_local_stack_is_complete_and_ready() -> None:
    stack = build_in_memory_local_stack(settings=Settings(environment_id="dev"))
    assert isinstance(stack, LocalStack)
    assert isinstance(stack.runtime._runner._gateway, DeterministicToolGateway)
    assert await stack.readiness.check() == ReadinessReport(
        database_ok=True,
        revision_matches_head=True,
        assembled=True,
    )
    await stack.aclose()


@pytest.mark.asyncio
async def test_worker_interface_closes_the_stack_when_cancelled() -> None:
    closed = asyncio.Event()
    stack = build_in_memory_local_stack(
        settings=Settings(environment_id="dev"),
        on_close=closed.set,
    )
    from xiaowei_agent.interfaces.worker import serve_worker

    stop = asyncio.Event()
    stop.set()
    assert await serve_worker(stack=stack, stop=stop) == 0
    assert closed.is_set()


def test_default_stack_does_not_install_a_smoke_barrier() -> None:
    stack = build_in_memory_local_stack(settings=Settings(environment_id="dev"))
    assert type(stack.runtime._runner._gateway) is DeterministicToolGateway


@pytest.mark.asyncio
async def test_packaged_recording_runs_one_complete_task_without_tests_data() -> None:
    import datetime as dt

    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = Settings(environment_id="dev")
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor="local-developer",
        environment_id=settings.environment_id,
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )
    submission = TaskSubmission(
        envelope=RequestEnvelope(
            request_id="request-1",
            tenant_id=context.tenant_id,
            actor=context.actor,
            channel=Channel.API,
            text="检查最近三十分钟慢查询",
            idempotency_key="idem-local-stack-1",
            environment_id=context.environment_id,
        ),
        context=context,
        as_of=now,
    )
    pending = await stack.runtime.submit_task(submission=submission)
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1
    completed = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.render is not None
    assert await stack.evidence_ledger.load(task_id=pending.task_id)

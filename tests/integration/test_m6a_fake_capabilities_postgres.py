"""M6a Prometheus fake 能力经真实 PostgreSQL 的持久化闭环。"""

import asyncio
import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces.local_stack import build_postgres_local_stack


async def test_prometheus_task_persists_and_reprojects_after_stack_restart(
    clean_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    settings = Settings(environment_id="dev")
    monkeypatch.setattr(
        local_stack_module,
        "create_database_engine",
        lambda _: clean_database,
    )
    first = await build_postgres_local_stack(
        settings=settings,
        clock=lambda: now,
        monotonic=lambda: 0.0,
    )
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="3" * 32,
        policy_revision=first.policy_revision,
    )
    pending = await first.runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id="m6a-postgres-prometheus",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text="查告警 HostHighCpu 在 node-1.example.com:9100 的证据",
                idempotency_key="m6a-postgres-prometheus",
                environment_id=context.environment_id,
            ),
            context=context,
            as_of=now,
        )
    )
    worker = WorkerLoop(
        runtime=first.runtime,
        task_store=first.task_store,
        clock=first.clock,
        monotonic=first.monotonic,
        settings=settings,
        sleep=asyncio.sleep,
    )
    assert await worker.poll_once() == 1
    lookup = TaskLookup(
        task_id=pending.task_id,
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
    )
    completed = await first.runtime.query_task(lookup=lookup)
    stored = await first.evidence_ledger.load(task_id=pending.task_id)

    restarted = await build_postgres_local_stack(
        settings=settings,
        clock=lambda: now,
        monotonic=lambda: 0.0,
    )
    reprojected = await restarted.runtime.query_task(lookup=lookup)

    assert completed.status is TaskStatus.SUCCEEDED
    assert len(stored) == 2
    assert reprojected == completed
    assert reprojected.render is not None
    assert "不是自动根因结论" in reprojected.render.answer

"""W5 发布预检在真实 PostgreSQL 上：recording 时代的成功结果也不继承，且全程只读。"""

import asyncio
import datetime as dt
import io
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from tests.fakes.clock import ManualClock

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
from xiaowei_agent.interfaces import release_preflight
from xiaowei_agent.persistence.postgres import PostgresTaskStore
from xiaowei_agent.persistence.schema import TASK_EVIDENCE, TASK_PLANS, TASKS
from xiaowei_agent.persistence.store import TransitionCommand

_NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)


def _config_root(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    for domain in ("ai", "feishu", "resources"):
        (root / domain).mkdir(parents=True)
    return root


def _submission(settings: Settings, *, key: str, text: str) -> TaskSubmission:
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor="local-developer",
        environment_id=settings.environment_id,
        trace_id="0" * 32,
        policy_revision=local_stack_module.ACTIVE_POLICY_SNAPSHOT.policy_revision,
    )
    return TaskSubmission(
        envelope=RequestEnvelope(
            request_id=f"request-{key}",
            tenant_id=context.tenant_id,
            actor=context.actor,
            channel=Channel.API,
            text=text,
            idempotency_key=f"idem-{key}",
            environment_id=context.environment_id,
        ),
        context=context,
        as_of=_NOW,
    )


async def _counts(engine: AsyncEngine) -> list[int]:
    counts: list[int] = []
    async with engine.connect() as connection:
        for table in (TASKS, TASK_PLANS, TASK_EVIDENCE):
            counts.append(
                int(
                    await connection.scalar(
                        sa.select(sa.func.count()).select_from(table)
                    )
                    or 0
                )
            )
    return counts


def _preflight(postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in list(os.environ):
        if name.startswith("XIAOWEI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XIAOWEI_ENVIRONMENT_ID", "dev")
    config_root = _config_root(tmp_path)

    async def run() -> tuple[int, dict[str, object]]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = await asyncio.to_thread(
            release_preflight.main,
            [],
            stdout=stdout,
            stderr=stderr,
            engine_factory=lambda _settings: create_async_engine(postgres_dsn),
            config_root=str(config_root),
        )
        assert stderr.getvalue() == ""
        return code, json.loads(stdout.getvalue())

    return run


async def test_empty_database_at_head_passes(
    clean_database, postgres_dsn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _preflight(postgres_dsn, tmp_path, monkeypatch)

    assert await run() == (
        0,
        {
            "result": "ok",
            "tasks_total": 0,
            "non_terminal_tasks": 0,
            "tasks_with_execution_data": 0,
        },
    )


async def test_recording_era_success_blocks_the_first_release_and_nothing_changes(
    clean_database, postgres_dsn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 用 offline_recording 的真实 worker 装配跑出一条 SUCCEEDED + plan + evidence：
    # 这正是"同一 dev-local/dev 里 recording 时代的合成结果"。
    monkeypatch.setattr(
        local_stack_module, "create_database_engine", lambda _: clean_database
    )
    settings = Settings(environment_id="dev")
    stack = await local_stack_module.build_postgres_local_stack(
        settings=settings, clock=ManualClock(start=_NOW)
    )
    pending = await stack.runtime.submit_task(
        submission=_submission(settings, key="recording", text="检查最近三十分钟慢查询")
    )
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )
    assert await worker.poll_once() == 1
    record = await stack.task_store.get(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
        )
    )
    assert record.status is TaskStatus.SUCCEEDED
    before = await _counts(clean_database)
    assert before[1] == 1 and before[2] >= 1
    run = _preflight(postgres_dsn, tmp_path, monkeypatch)

    first = await run()
    second = await run()

    assert first == second == (
        1,
        {
            "result": "historical_execution_data_present",
            "tasks_total": 1,
            "non_terminal_tasks": 0,
            "tasks_with_execution_data": 1,
        },
    )
    assert pending.task_id not in json.dumps(first)
    assert await _counts(clean_database) == before


async def test_a_planning_task_beyond_the_first_page_is_not_drained(
    clean_database, postgres_dsn, clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(environment_id="dev")
    store = PostgresTaskStore(engine=clean_database, clock=clock)
    oldest = await store.create_task(
        submission=_submission(settings, key="oldest", text="你能做什么？")
    )
    grant = await store.acquire_lease(task_id=oldest.task_id, owner="probe", ttl_seconds=60)
    assert grant is not None
    moved = await store.transition(
        command=TransitionCommand(
            task_id=oldest.task_id,
            expected_version=oldest.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=grant.fencing_token,
        )
    )
    assert moved.applied
    for index in range(120):
        created = await store.create_task(
            submission=_submission(settings, key=f"late-{index}", text="你能做什么？")
        )
        rejected = await store.transition(
            command=TransitionCommand(
                task_id=created.task_id,
                expected_version=created.version,
                to_status=TaskStatus.REJECTED,
                terminal_reason="resolver.rejected",
            )
        )
        assert rejected.applied, rejected.rejection
    run = _preflight(postgres_dsn, tmp_path, monkeypatch)

    code, report = await run()

    assert code == 1
    assert report == {
        "result": "tasks_not_drained",
        "tasks_total": 121,
        "non_terminal_tasks": 1,
        "tasks_with_execution_data": 0,
    }

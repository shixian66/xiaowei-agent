"""F1 SQL 消息经渠道提交 —— **PostgreSQL**（设计 §5.1、§5.2）。

真库证明：SQL 消息只写 ``sql_artifacts`` 一处原文；task、submission、渠道绑定与通知订阅
行都不含 SQL；同一渠道幂等键重放得到同一任务，不重复写 SqlArtifact；同一渠道事件无论
顺序还是两连接并发，都只留下一种提交类型的一个任务与一个绑定。
"""

import asyncio
import datetime as dt
import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.application.channel_submission import (
    ChannelSubmissionService,
    ChannelSubmitCommand,
)
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    IdentitySource,
)
from xiaowei_agent.governance.sql_message import recognize_sql_message
from xiaowei_agent.persistence import IdempotencyConflictError
from xiaowei_agent.persistence.postgres import (
    PostgresChannelStore,
    PostgresEvidenceLedger,
    PostgresPlanStore,
)
from xiaowei_agent.persistence.schema import (
    CHANNEL_BINDINGS,
    PROJECTION_SUBSCRIPTIONS,
    SQL_ARTIFACTS,
    TASK_SUBMISSIONS,
    TASKS,
)

_SQL = "SELECT secret_marker_column FROM orders"
_NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)


def _command(
    *, request_id: str = "r1", text: str = _SQL, ref: str = "event-sql-1"
) -> ChannelSubmitCommand:
    return ChannelSubmitCommand(
        principal=AuthenticatedPrincipal(
            tenant_id="dev-local",
            environment_id="dev",
            actor="alice",
            source=IdentitySource.FEISHU,
            subject_ref="subject-alice",
            permissions=frozenset(
                {ChannelPermission.VIEW_SAFE_TASK, ChannelPermission.SUBMIT_READONLY_TASK}
            ),
        ),
        channel=ChannelKind.FEISHU_GROUP,
        request_id=request_id,
        trace_id="1" * 32,
        policy_revision="policy-1",
        text=text,
        client_submission_ref=ref,
        conversation_ref="chat-1",
        submitted_at=_NOW,
    )


async def _rows(engine: AsyncEngine, table: Any) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(sa.select(table))
        return [dict(row) for row in result.mappings()]


def _service(store: Any, engine: AsyncEngine, clock: Any) -> ChannelSubmissionService:
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=PostgresPlanStore(engine=engine),
        ledger=PostgresEvidenceLedger(engine=engine),
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),  # type: ignore[arg-type]
        recognize_sql=recognize_sql_message,
    )
    return ChannelSubmissionService(
        runtime=runtime,
        channel_store=PostgresChannelStore(engine=engine, clock=clock),
    )


async def test_sql_message_is_stored_once_and_nowhere_else(
    store: Any, clean_database: AsyncEngine, clock: Any
) -> None:
    service = _service(store, clean_database, clock)

    first = await service.submit(command=_command())
    replay = await service.submit(command=_command(request_id="r2"))

    assert replay.task_view.task_id == first.task_view.task_id
    artifacts = await _rows(clean_database, SQL_ARTIFACTS)
    assert len(artifacts) == 1
    assert bytes(artifacts[0]["sql_bytes"]) == _SQL.encode()
    submissions = await _rows(clean_database, TASK_SUBMISSIONS)
    assert len(submissions) == 1
    assert submissions[0]["input_kind"] == "sql_artifact"
    assert submissions[0]["sql_ref"] == artifacts[0]["sql_ref"]
    for table in (TASKS, TASK_SUBMISSIONS, CHANNEL_BINDINGS, PROJECTION_SUBSCRIPTIONS):
        rows = await _rows(clean_database, table)
        assert rows, table.name
        assert "secret_marker_column" not in json.dumps(rows, default=str), table.name


_CONVERSATION = "inspect slow queries"


async def _unbound_tasks(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(TASKS.c.task_id).where(
                ~sa.exists().where(CHANNEL_BINDINGS.c.task_id == TASKS.c.task_id)
            )
        )
        return list(result.scalars())


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [(_CONVERSATION, _SQL), (_SQL, _CONVERSATION)],
    ids=["conversation-then-sql", "sql-then-conversation"],
)
async def test_one_channel_event_keeps_one_kind_in_postgres(
    store: Any, clean_database: AsyncEngine, clock: Any, first_text: str, second_text: str
) -> None:
    service = _service(store, clean_database, clock)
    first = await service.submit(command=_command(text=first_text))

    with pytest.raises(IdempotencyConflictError):
        await service.submit(command=_command(request_id="r2", text=second_text))

    tasks = await _rows(clean_database, TASKS)
    assert [row["task_id"] for row in tasks] == [first.task_view.task_id]
    assert len(await _rows(clean_database, SQL_ARTIFACTS)) == (1 if first_text == _SQL else 0)
    assert len(await _rows(clean_database, CHANNEL_BINDINGS)) == 1
    assert await _unbound_tasks(clean_database) == []


async def test_concurrent_cross_kind_submissions_of_one_event_leave_one_task(
    store: Any, clean_database: AsyncEngine, clock: Any
) -> None:
    # 两个服务实例经连接池各用独立连接；每一轮都是同一来源事件的一对跨类型并发提交。
    left = _service(store, clean_database, clock)
    right = _service(store, clean_database, clock)
    rounds = 12
    for index in range(rounds):
        results = await asyncio.gather(
            left.submit(command=_command(text=_CONVERSATION, ref=f"race-{index}")),
            right.submit(command=_command(request_id="r2", text=_SQL, ref=f"race-{index}")),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1, results
        assert len(losers) == 1 and isinstance(losers[0], IdempotencyConflictError), results

    tasks = await _rows(clean_database, TASKS)
    bindings = await _rows(clean_database, CHANNEL_BINDINGS)
    submissions = await _rows(clean_database, TASK_SUBMISSIONS)
    assert len(tasks) == len(bindings) == rounds
    assert await _unbound_tasks(clean_database) == []
    sql_tasks = {row["sql_ref"] for row in submissions if row["input_kind"] == "sql_artifact"}
    artifacts = {row["sql_ref"] for row in await _rows(clean_database, SQL_ARTIFACTS)}
    # 没有孤儿 SqlArtifact：每一份都属于一个已绑定的 SQL 任务。
    assert artifacts == sql_tasks

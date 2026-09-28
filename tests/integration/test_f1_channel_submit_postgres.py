"""F1 SQL 消息经渠道提交 —— **PostgreSQL**（设计 §5.1、§5.2）。

真库证明：SQL 消息只写 ``sql_artifacts`` 一处原文；task、submission、渠道绑定与通知订阅
行都不含 SQL；同一渠道幂等键重放得到同一任务，不重复写 SqlArtifact。
"""

import datetime as dt
import json
from typing import Any

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


def _command(*, request_id: str = "r1") -> ChannelSubmitCommand:
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
        text=_SQL,
        client_submission_ref="event-sql-1",
        conversation_ref="chat-1",
        submitted_at=_NOW,
    )


async def _rows(engine: AsyncEngine, table: Any) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(sa.select(table))
        return [dict(row) for row in result.mappings()]


async def test_sql_message_is_stored_once_and_nowhere_else(
    store: Any, clean_database: AsyncEngine, clock: Any
) -> None:
    runtime = TaskViewRuntime(
        task_store=store,
        plan_store=PostgresPlanStore(engine=clean_database),
        ledger=PostgresEvidenceLedger(engine=clean_database),
        conversation_snapshot=StaticCapabilityRegistry().snapshot(),
        rendering_bindings=object(),  # type: ignore[arg-type]
    )
    service = ChannelSubmissionService(
        recognize_sql=recognize_sql_message,
        runtime=runtime,
        channel_store=PostgresChannelStore(engine=clean_database, clock=clock),
    )

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

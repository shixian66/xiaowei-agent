"""F1 SQL 提交与水合 —— **内存绑定**。

与 ``tests/integration/test_f1_submit_postgres.py`` 跑同一批共享用例；另外证明内存实现
对已清除的 tombstone 与更晚的过期时间给出与 PostgreSQL 相同的判定。
"""

import dataclasses
import datetime as dt
from typing import Any

import pytest
from tests.suites.task_store import F1_SQL_CASES, _sql_attempt, _sql_task_grant, bind

from xiaowei_agent.persistence.store import (
    GrantNotCurrentError,
    SqlArtifactExpiredError,
)

bind(globals(), F1_SQL_CASES)

_SQL = b"SELECT id FROM orders"


async def _grant(store: Any, context: Any, *, key: str) -> Any:
    _, attempt = await _sql_task_grant(store, context, key=key)
    return attempt


async def test_a_stale_grant_neither_reads_nor_extends_the_sql(
    store: Any, clock: Any, context: Any, memory_state: Any
) -> None:
    result, stale = await _sql_task_grant(store, context, key="sql-stale")
    submission = stale.submission
    before = memory_state.sql_artifacts[submission.sql_ref]

    async def load_with_stale_grant() -> None:
        with pytest.raises(GrantNotCurrentError):
            await store.load_sql_for_execution(
                grant=stale.grant, sql_ref=submission.sql_ref, sql_hash=submission.sql_hash
            )

    clock.advance(seconds=61)
    await load_with_stale_grant()  # 租约已过期
    assert memory_state.sql_artifacts[submission.sql_ref] == before

    await _sql_attempt(store, result.task.task_id, owner="worker-next")
    await load_with_stale_grant()  # 已被新 worker 接管
    assert memory_state.sql_artifacts[submission.sql_ref] == before


async def test_a_purged_tombstone_is_expired(
    store: Any, clock: Any, context: Any, memory_state: Any
) -> None:
    attempt = await _grant(store, context, key="sql-purged")
    submission = attempt.submission
    row = memory_state.sql_artifacts[submission.sql_ref]
    memory_state.sql_artifacts[submission.sql_ref] = dataclasses.replace(
        row, sql_bytes=None, purged_at=clock()
    )

    with pytest.raises(SqlArtifactExpiredError):
        await store.load_sql_for_execution(
            grant=attempt.grant, sql_ref=submission.sql_ref, sql_hash=submission.sql_hash
        )


async def test_hydration_never_shortens_a_later_expiry(
    store: Any, clock: Any, context: Any, memory_state: Any
) -> None:
    attempt = await _grant(store, context, key="sql-later")
    submission = attempt.submission
    later = clock() + dt.timedelta(days=5)
    row = memory_state.sql_artifacts[submission.sql_ref]
    memory_state.sql_artifacts[submission.sql_ref] = dataclasses.replace(
        row, expires_at=later
    )

    loaded = await store.load_sql_for_execution(
        grant=attempt.grant, sql_ref=submission.sql_ref, sql_hash=submission.sql_hash
    )
    assert loaded.expires_at == later
    assert memory_state.sql_artifacts[submission.sql_ref].expires_at == later

"""F1 SQL 提交与水合 —— **PostgreSQL 绑定**，外加只有真库才能证明的事务性质。

共享用例（``F1_SQL_CASES``）与内存实现跑同一批函数对象；本文件另外证明：
SqlArtifact、task 与 submission 在**同一个事务**里提交（故障注入全部回滚、提交结果
未知时重试回读 winner 不重复创建、并发同键只有一个 winner），以及数据库约束与
``GREATEST`` 只延不缩。
"""

import asyncio
import datetime as dt
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from tests.conftest import lookup_for, make_envelope, make_submission
from tests.suites.task_store import F1_SQL_CASES, _sql_attempt, _sql_task_grant, bind

from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.postgres import PostgresTaskStore
from xiaowei_agent.persistence.schema import (
    SQL_ARTIFACTS,
    TASK_SUBMISSIONS,
    TASKS,
)
from xiaowei_agent.persistence.store import (
    SQL_ARTIFACT_TTL,
    GrantNotCurrentError,
    SqlArtifactExpiredError,
    SqlQuerySubmitCommand,
    SqlSubmitOutcome,
)

bind(globals(), F1_SQL_CASES)

_SQL = b"SELECT id FROM orders"
_AS_OF = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)


def _command(context: Any, *, key: str = "sql-1", sql: bytes = _SQL) -> SqlQuerySubmitCommand:
    return SqlQuerySubmitCommand(
        context=context, sql_bytes=sql, idempotency_key=key, as_of=_AS_OF
    )


async def _counts(engine: AsyncEngine) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        return tuple(  # type: ignore[return-value]
            [
                await connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (SQL_ARTIFACTS, TASKS, TASK_SUBMISSIONS)
            ]
        )


@asynccontextmanager
async def _failing_insert(engine: AsyncEngine, table: str) -> AsyncIterator[None]:
    """在指定表的 INSERT 上注入失败；退出时移除。"""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "CREATE FUNCTION f1_reject_insert() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'F1_FAULT_INJECTION'; END; $$ LANGUAGE plpgsql"
            )
        )
        await connection.execute(
            sa.text(
                f"CREATE TRIGGER f1_reject_insert BEFORE INSERT ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION f1_reject_insert()"
            )
        )
    try:
        yield
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f"DROP TRIGGER f1_reject_insert ON {table}"))
            await connection.execute(sa.text("DROP FUNCTION f1_reject_insert()"))


@pytest.mark.parametrize("table", ["sql_artifacts", "tasks", "task_submissions"])
async def test_a_failure_at_any_write_rolls_back_the_whole_submit(
    clean_database: AsyncEngine, store: Any, context: Any, table: str
) -> None:
    async with _failing_insert(clean_database, table):
        with pytest.raises(PersistenceIntegrityError) as caught:
            await store.submit_sql_query(command=_command(context))
        assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
        assert caught.value.__context__ is None
        assert await _counts(clean_database) == (0, 0, 0)

    retried = await store.submit_sql_query(command=_command(context))
    assert retried.outcome is SqlSubmitOutcome.CREATED
    assert await _counts(clean_database) == (1, 1, 1)


class _AckLostEngine:
    """提交成功后才报连接错误：调用方只知道“结果未知”。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.armed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.begin() as connection:
            yield connection
        if self.armed:
            self.armed = False
            raise sa.exc.OperationalError("COMMIT", {}, ConnectionResetError())


async def test_an_unconfirmed_commit_is_resolved_by_replaying_the_winner(
    clean_database: AsyncEngine, clock: Any, context: Any
) -> None:
    lost_ack = PostgresTaskStore(
        engine=_AckLostEngine(clean_database),  # type: ignore[arg-type]
        clock=clock,
    )
    with pytest.raises(PersistenceUnavailableError) as caught:
        await lost_ack.submit_sql_query(command=_command(context))
    assert caught.value.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED

    retried = await lost_ack.submit_sql_query(command=_command(context))
    assert retried.outcome is SqlSubmitOutcome.REPLAYED
    assert await _counts(clean_database) == (1, 1, 1)


async def test_a_commit_that_fails_leaves_nothing_and_the_retry_creates_once(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "CREATE FUNCTION f1_reject_commit() RETURNS trigger AS $$ "
                "BEGIN RAISE EXCEPTION 'F1_COMMIT_FAULT'; END; $$ LANGUAGE plpgsql"
            )
        )
        await connection.execute(
            sa.text(
                "CREATE CONSTRAINT TRIGGER f1_reject_commit AFTER INSERT ON task_submissions "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION f1_reject_commit()"
            )
        )
    try:
        with pytest.raises(PersistenceIntegrityError) as caught:
            await store.submit_sql_query(command=_command(context))
        assert caught.value.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED
    finally:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("DROP TRIGGER f1_reject_commit ON task_submissions")
            )
            await connection.execute(sa.text("DROP FUNCTION f1_reject_commit()"))

    assert await _counts(clean_database) == (0, 0, 0)
    retried = await store.submit_sql_query(command=_command(context))
    assert retried.outcome is SqlSubmitOutcome.CREATED
    assert await _counts(clean_database) == (1, 1, 1)


async def test_concurrent_submits_with_one_key_have_exactly_one_winner(
    clean_database: AsyncEngine, independent_stores: Any, context: Any
) -> None:
    stores = independent_stores(6)
    results = await asyncio.gather(
        *(store.submit_sql_query(command=_command(context)) for store in stores)
    )

    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes == ["created"] + ["replayed"] * 5
    assert len({result.task.task_id for result in results}) == 1
    assert await _counts(clean_database) == (1, 1, 1)


async def test_concurrent_different_sql_under_one_key_conflicts_without_orphans(
    clean_database: AsyncEngine, independent_stores: Any, context: Any
) -> None:
    stores = independent_stores(4)
    results = await asyncio.gather(
        *(
            store.submit_sql_query(command=_command(context, sql=f"SELECT {i}".encode()))
            for i, store in enumerate(stores)
        )
    )

    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes == ["created"] + ["idempotency_conflict"] * 3
    # 输家写过的 SqlArtifact 随事务回滚，不留孤儿行。
    assert await _counts(clean_database) == (1, 1, 1)


async def _grant(store: Any, context: Any, *, key: str) -> Any:
    _, attempt = await _sql_task_grant(store, context, key=key)
    return attempt


async def _stored_expiry(engine: AsyncEngine, sql_ref: str) -> dt.datetime:
    async with engine.connect() as connection:
        value = await connection.scalar(
            sa.select(SQL_ARTIFACTS.c.expires_at).where(SQL_ARTIFACTS.c.sql_ref == sql_ref)
        )
    assert isinstance(value, dt.datetime)
    return value


async def test_a_stale_grant_neither_reads_nor_extends_the_sql(
    clean_database: AsyncEngine, store: Any, clock: Any, context: Any
) -> None:
    result, stale = await _sql_task_grant(store, context, key="sql-stale")
    submission = stale.submission
    before = await _stored_expiry(clean_database, submission.sql_ref)

    async def load_with_stale_grant() -> None:
        with pytest.raises(GrantNotCurrentError):
            await store.load_sql_for_execution(
                grant=stale.grant, sql_ref=submission.sql_ref, sql_hash=submission.sql_hash
            )

    clock.advance(seconds=61)
    await load_with_stale_grant()  # 租约已过期
    assert await _stored_expiry(clean_database, submission.sql_ref) == before

    await _sql_attempt(store, result.task.task_id, owner="worker-next")
    await load_with_stale_grant()  # 已被新 worker 接管
    assert await _stored_expiry(clean_database, submission.sql_ref) == before


async def test_a_purged_tombstone_is_expired_even_before_its_expiry_time(
    clean_database: AsyncEngine, store: Any, clock: Any, context: Any
) -> None:
    attempt = await _grant(store, context, key="sql-purged")
    submission = attempt.submission
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.update(SQL_ARTIFACTS)
            .where(SQL_ARTIFACTS.c.sql_ref == submission.sql_ref)
            .values(sql_bytes=None, purged_at=clock())
        )

    with pytest.raises(SqlArtifactExpiredError):
        await store.load_sql_for_execution(
            grant=attempt.grant, sql_ref=submission.sql_ref, sql_hash=submission.sql_hash
        )


async def test_hydration_never_shortens_a_later_expiry(
    clean_database: AsyncEngine, store: Any, clock: Any, context: Any
) -> None:
    attempt = await _grant(store, context, key="sql-later")
    submission = attempt.submission
    later = clock() + dt.timedelta(days=5)
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.update(SQL_ARTIFACTS)
            .where(SQL_ARTIFACTS.c.sql_ref == submission.sql_ref)
            .values(expires_at=later)
        )

    loaded = await store.load_sql_for_execution(
        grant=attempt.grant, sql_ref=submission.sql_ref, sql_hash=submission.sql_hash
    )
    assert loaded.expires_at == later

    async with clean_database.connect() as connection:
        stored = await connection.scalar(
            sa.select(SQL_ARTIFACTS.c.expires_at).where(
                SQL_ARTIFACTS.c.sql_ref == submission.sql_ref
            )
        )
    assert stored == later


async def test_submit_stores_bytes_hash_and_a_24_hour_expiry(
    clean_database: AsyncEngine, store: Any, clock: Any, context: Any
) -> None:
    result = await store.submit_sql_query(command=_command(context))
    submission = await store.get_submission(lookup=lookup_for(result.task))
    async with clean_database.connect() as connection:
        row = (
            await connection.execute(
                sa.select(SQL_ARTIFACTS).where(
                    SQL_ARTIFACTS.c.sql_ref == submission.sql_ref
                )
            )
        ).mappings().one()
    assert bytes(row["sql_bytes"]) == _SQL
    assert row["sql_hash"] == hashlib.sha256(_SQL).hexdigest()
    assert (row["requester"], row["tenant_id"], row["environment_id"]) == (
        context.actor,
        context.tenant_id,
        context.environment_id,
    )
    assert row["created_at"] == clock()
    assert row["expires_at"] == clock() + SQL_ARTIFACT_TTL
    assert row["purged_at"] is None


# ---- 数据库约束独立表达同一不变量 ----


async def _rejected(engine: AsyncEngine, statement: str, **params: Any) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(sa.text(statement), params)


async def test_purge_shape_constraint_requires_exactly_one_of_bytes_or_purged_at(
    clean_database: AsyncEngine,
) -> None:
    insert = (
        "INSERT INTO sql_artifacts (sql_ref, sql_hash, sql_bytes, requester, tenant_id, "
        "environment_id, created_at, expires_at, purged_at) VALUES "
        "(:ref, :hash, :bytes, 'alice', 'dev-local', 'dev', now(), now(), :purged)"
    )
    digest = hashlib.sha256(_SQL).hexdigest()
    await _rejected(clean_database, insert, ref="both-null", hash=digest, bytes=None, purged=None)
    await _rejected(
        clean_database,
        insert,
        ref="both-set",
        hash=digest,
        bytes=_SQL,
        purged=dt.datetime(2026, 9, 28, tzinfo=dt.UTC),
    )
    await _rejected(clean_database, insert, ref="empty", hash=digest, bytes=b"", purged=None)


async def test_submission_shape_constraint_rejects_mixed_rows(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    conversation = await store.create_task(submission=make_submission(context))
    sql = await store.submit_sql_query(command=_command(context))
    sql_submission = await store.get_submission(lookup=lookup_for(sql.task))
    envelope = make_envelope().model_dump_json()

    update = "UPDATE task_submissions SET {assignment} WHERE task_id = :task_id"
    for task_id, assignment, params in (
        (conversation.task_id, "sql_ref = :ref", {"ref": sql_submission.sql_ref}),
        (conversation.task_id, "sql_hash = :hash", {"hash": sql_submission.sql_hash}),
        (conversation.task_id, "envelope = NULL", {}),
        (sql.task.task_id, "envelope = CAST(:envelope AS jsonb)", {"envelope": envelope}),
        (sql.task.task_id, "sql_hash = NULL", {}),
        (sql.task.task_id, "sql_ref = NULL", {}),
        (sql.task.task_id, "clarification_parent_task_id = :parent", {
            "parent": conversation.task_id
        }),
        (conversation.task_id, "input_kind = 'raw_sql'", {}),
    ):
        await _rejected(
            clean_database,
            update.format(assignment=assignment),
            task_id=task_id,
            **params,
        )


async def test_only_requester_owner_grants_may_omit_the_approval_ref(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    sql = await store.submit_sql_query(command=_command(context))
    sql_submission = await store.get_submission(lookup=lookup_for(sql.task))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO query_results (result_ref, task_id, requester, tenant_id, "
                "environment_id, target_fingerprint, config_revision, sql_ref, sql_hash, "
                "query_id, started_at, finished_at, columns, rows, saved_rows, saved_bytes, "
                "has_more, completeness, created_at, expires_at, export_policy) VALUES "
                "('result-1', :task_id, 'alice', 'dev-local', 'dev', :fp, 'rev', :ref, "
                ":hash, NULL, now(), now(), '[]', '[]', 0, 0, false, 'complete', now(), "
                "now(), 'disabled')"
            ),
            {
                "task_id": sql.task.task_id,
                "fp": "f" * 64,
                "ref": sql_submission.sql_ref,
                "hash": sql_submission.sql_hash,
            },
        )
        await connection.execute(
            sa.text(
                "INSERT INTO result_access_grants (result_ref, principal, grant_kind, "
                "approval_ref, created_at, expires_at) VALUES "
                "('result-1', 'alice', 'requester_owner', NULL, now(), now())"
            )
        )
    grant = (
        "INSERT INTO result_access_grants (result_ref, principal, grant_kind, "
        "approval_ref, created_at, expires_at) VALUES "
        "('result-1', 'bob', :kind, NULL, now(), now())"
    )
    await _rejected(clean_database, grant, kind="view_approver")
    await _rejected(clean_database, grant, kind="export_approver")
    await _rejected(clean_database, grant, kind="admin")

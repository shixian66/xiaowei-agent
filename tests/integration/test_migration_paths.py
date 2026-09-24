"""两条 upgrade 路径都要验证（DEVELOPMENT_PLAN §7 M4 测试门明文）。

空库能建起来只证明了一半：真正会出问题的是**已有数据**的库——一条加了 NOT NULL
但没给默认值的列、一条与既有行冲突的 CHECK，都只在有数据时才炸，而那正是生产
环境的形态。
"""

from typing import Any

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.contracts import TaskStatus
from xiaowei_agent.persistence.migrations.guards import (
    MigrationPreconditionError,
    MigrationSafetyError,
)
from xiaowei_agent.persistence.migrations.runner import alembic_config
from xiaowei_agent.persistence.schema import (
    ALL_TABLES,
    CREATED_SEQUENCE_NAME,
    FENCING_SEQUENCE_NAME,
)


def _head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        )
        return {row[0] for row in rows}


async def test_upgrade_on_an_empty_database_creates_every_table(
    clean_database: AsyncEngine,
) -> None:
    present = await _table_names(clean_database)
    assert {table.name for table in ALL_TABLES} <= present


async def test_upgrade_creates_the_fencing_sequence(clean_database: AsyncEngine) -> None:
    async with clean_database.connect() as connection:
        rows = await connection.execute(
            sa.text("SELECT sequencename FROM pg_sequences WHERE schemaname = current_schema()")
        )
        names = {row[0] for row in rows}
        assert FENCING_SEQUENCE_NAME in names
        assert CREATED_SEQUENCE_NAME in names


async def test_rev_0002_round_trips_m4_data_and_restores_the_old_conflict_target(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any], store: Any, context: Any
) -> None:
    """降到 M4 后，旧代码使用的约束名与 SQL 形状都必须真的可用。"""
    from tests.conftest import make_submission

    await store.create_task(submission=make_submission(context))
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0001_initial", True)
        constraints = await connection.execute(
            sa.text("SELECT conname FROM pg_constraint WHERE conrelid = 'tasks'::regclass")
        )
        assert "uq_tasks_idempotency_scope" in {row[0] for row in constraints}
        await connection.execute(
            sa.text(
                "INSERT INTO tasks "
                "(task_id, tenant_id, environment_id, actor, idempotency_key, "
                "request_digest, status, version) "
                "VALUES (:task_id, :tenant_id, :environment_id, :actor, :key, "
                ":digest, 'created', 0) "
                "ON CONFLICT ON CONSTRAINT uq_tasks_idempotency_scope DO NOTHING"
            ),
            {
                "task_id": "m4-shape-task",
                "tenant_id": context.tenant_id,
                "environment_id": context.environment_id,
                "actor": context.actor,
                "key": "m4-shape-key",
                "digest": "d" * 64,
            },
        )
        await connection.run_sync(run_upgrade, "head")

    async with clean_database.connect() as connection:
        row = await connection.execute(
            sa.text(
                "SELECT created_seq, attempt_number, task_failure_count, next_attempt_at "
                "FROM tasks WHERE task_id = 'm4-shape-task'"
            )
        )
        values = row.one()
        assert values[0] > 0
        assert values[1:] == (0, 0, None)


async def test_downgrade_then_upgrade_over_a_database_with_data(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any], store: Any, context: Any
) -> None:
    """有数据的库：先写入真实数据，再 downgrade 到底，再 upgrade 回来。

    ``downgrade`` 会连数据一起删除——这是**预期**行为，本条断言的是"回滚不会卡在
    残留对象上"，不是"数据能存活"。残留的表会让下一次 upgrade 在"已存在"上失败，
    把一次可回滚的迁移变成需要人工清理的死局。
    """
    from tests.conftest import make_submission

    await store.create_task(submission=make_submission(context))
    async with clean_database.connect() as connection:
        count = await connection.execute(sa.text("SELECT count(*) FROM tasks"))
        assert count.scalar_one() == 1

    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "base", True)
    assert not ({table.name for table in ALL_TABLES} & await _table_names(clean_database))

    async with clean_database.begin() as connection:
        await connection.run_sync(run_upgrade)
    assert {table.name for table in ALL_TABLES} <= await _table_names(clean_database)


async def test_lease_check_constraint_is_enforced_by_the_database(
    clean_database: AsyncEngine, store: Any, context: Any
) -> None:
    """数据库层的 CHECK 与契约层的校验器是**两层独立表达**。

    绕过 ``TaskStore`` 直接写半置位的租约必须被数据库拒绝——只有这样"两层"才不是
    一句空话。测试可以够到存储细节，生产代码不行。
    """
    from tests.conftest import make_submission

    record = await store.create_task(submission=make_submission(context))
    with pytest.raises(sa.exc.IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("UPDATE tasks SET lease_owner = 'w1' WHERE task_id = :task_id"),
                {"task_id": record.task_id},
            )


async def test_rev_0003_settles_only_non_terminal_m4_tasks_without_audit(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any]
) -> None:
    """历史提交事实不可伪造；迁移只终态化仍可能执行的任务。"""
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0002_task_execution_columns")
        for task_id, status, version, created_seq in (
            ("legacy-active", "created", 3, 1001),
            ("legacy-terminal", "succeeded", 5, 1002),
        ):
            await connection.execute(
                sa.text(
                    "INSERT INTO tasks "
                    "(task_id, tenant_id, environment_id, actor, idempotency_key, "
                    "request_digest, status, version, created_seq, "
                    "idempotency_scope_digest) VALUES "
                    "(:task_id, 'tenant-a', 'dev', 'alice', :key, :digest, :status, "
                    ":version, :created_seq, :scope_digest)"
                ),
                {
                    "task_id": task_id,
                    "key": f"key-{task_id}",
                    "digest": "d" * 64,
                    "status": status,
                    "version": version,
                    "created_seq": created_seq,
                    "scope_digest": ("a" if task_id == "legacy-active" else "b") * 64,
                },
            )
        await connection.run_sync(run_upgrade)

    async with clean_database.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT task_id, status, version, terminal_reason FROM tasks "
                    "WHERE task_id LIKE 'legacy-%' ORDER BY task_id"
                )
            )
        ).all()
        audit_count = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM task_audit_events "
                "WHERE task_id IN ('legacy-active', 'legacy-terminal')"
            )
        )
    assert rows == [
        ("legacy-active", "failed", 4, "legacy_task_without_submission"),
        ("legacy-terminal", "succeeded", 5, None),
    ]
    assert audit_count == 0


async def test_rev_0003_downgrade_rejects_submission_data_by_default(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import make_submission

    await store.create_task(submission=make_submission(context))
    _, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0002_task_execution_columns")

    async with clean_database.connect() as connection:
        assert await connection.scalar(sa.text("SELECT count(*) FROM task_submissions")) == 1
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert revision == _head_revision()


async def test_explicit_rev_0003_downgrade_settles_active_m5_data(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(
            run_downgrade, "0002_task_execution_columns", True
        )
        row = (
            await connection.execute(
                sa.text(
                    "SELECT status, terminal_reason FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
        assert row == ("failed", "m5_downgrade_discarded")
        await connection.run_sync(run_upgrade)
        restored = (
            await connection.execute(
                sa.text(
                    "SELECT status, terminal_reason FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
    assert restored == ("failed", "m5_downgrade_discarded")


async def test_rev_0005_downgrade_requires_authorization_and_settles_active_data(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    """A step journal row cannot be discarded while its task still looks runnable."""
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO task_step_executions "
                "(task_id, step_id, attempt_count, last_fencing_token, started_at) "
                "VALUES (:task_id, 's1', 1, 1, now())"
            ),
            {"task_id": task.task_id},
        )

    run_upgrade, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0004_retry_markers")

    async with clean_database.connect() as connection:
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        step_count = await connection.scalar(
            sa.text("SELECT count(*) FROM task_step_executions")
        )
    assert revision == _head_revision()
    assert step_count == 1

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0004_retry_markers", True)
        settled = (
            await connection.execute(
                sa.text(
                    "SELECT status, terminal_reason FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
        await connection.run_sync(run_upgrade)
    assert settled == ("failed", "m5_downgrade_discarded")

    async with clean_database.connect() as connection:
        assert (
            await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM task_step_executions WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
            == 0
        )


async def test_rev_0004_retry_columns_round_trip_without_changing_task_facts(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    task: Any,
) -> None:
    """rev4 的 downgrade/upgrade 只移除重试幂等标记，不改既有任务事实。"""
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        before = (
            await connection.execute(
                sa.text(
                    "SELECT task_id, status, version, attempt_number, task_failure_count "
                    "FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
        await connection.run_sync(run_downgrade, "0003_task_submissions")
        columns_after_down = {
            row[0]
            for row in (
                await connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'tasks'"
                    )
                )
            ).all()
        }
        assert "retry_scheduled_by_attempt" not in columns_after_down
        assert "retry_command_digest" not in columns_after_down

        await connection.run_sync(run_upgrade)
        after = (
            await connection.execute(
                sa.text(
                    "SELECT task_id, status, version, attempt_number, task_failure_count "
                    "FROM tasks WHERE task_id = :task_id"
                ),
                {"task_id": task.task_id},
            )
        ).one()
    assert after == before


async def test_rev_0008_downgrade_requires_authorization_for_model_artifacts(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    """默认降级不能静默删除 intent/advisory；显式授权后可完整往返。"""
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO task_interaction_artifacts "
                "(task_id, artifact_version, draft, origin, provider, model, "
                "provider_origin, prompt_revision, schema_revision, input_digest, "
                "result_digest, usage, created_at, fencing_token) VALUES "
                "(:task_id, 1, CAST(:draft AS jsonb), 'rule', NULL, NULL, NULL, "
                "'intent-p1', 'intent-s1', :input_digest, :result_digest, "
                "CAST(:usage AS jsonb), now(), 1)"
            ),
            {
                "task_id": task.task_id,
                "draft": '{"source":"user","operation":"inspect"}',
                "input_digest": "a" * 64,
                "result_digest": "b" * 64,
                "usage": '{"input_tokens":0,"output_tokens":0}',
            },
        )
        await connection.execute(
            sa.text(
                "INSERT INTO task_model_advisories "
                "(task_id, artifact_version, advisory, origin, provider, model, "
                "provider_origin, prompt_revision, schema_revision, input_digest, "
                "result_digest, usage, created_at, fencing_token) VALUES "
                "(:task_id, 1, CAST(:advisory AS jsonb), 'model', 'gemini', "
                "'gemini-test', 'https://example.invalid', 'advisory-p1', "
                "'advisory-s1', :input_digest, :result_digest, "
                "CAST(:usage AS jsonb), now(), 1)"
            ),
            {
                "task_id": task.task_id,
                "advisory": '{"summary":"safe test advisory"}',
                "input_digest": "c" * 64,
                "result_digest": "d" * 64,
                "usage": '{"input_tokens":1,"output_tokens":1}',
            },
        )

    run_upgrade, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0007_web_sessions")

    assert exc_info.value.counts == (
        ("task_model_advisories", 1),
        ("task_accepted_intents", 1),
    )
    async with clean_database.connect() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        interaction_count = await connection.scalar(
            sa.text("SELECT count(*) FROM task_interaction_artifacts")
        )
        advisory_count = await connection.scalar(
            sa.text("SELECT count(*) FROM task_model_advisories")
        )
    assert revision == _head_revision()
    assert (interaction_count, advisory_count) == (1, 1)

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0007_web_sessions", True)
    assert not {
        "task_accepted_intents",
        "task_interaction_artifacts",
        "task_model_advisories",
    } & await _table_names(clean_database)

    async with clean_database.begin() as connection:
        await connection.run_sync(run_upgrade)
    assert {
        "task_interaction_artifacts",
        "task_model_advisories",
    } <= await _table_names(clean_database)


async def test_rev_0009_upgrade_preserves_existing_submission_with_null_parent(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import lookup_for, make_submission

    task = await store.create_task(submission=make_submission(context))
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0008_model_artifacts")
        columns = await connection.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'task_submissions'"
            )
        )
        names = {row[0] for row in columns}
        assert "parent_task_id" not in names
        assert "clarification_parent_task_id" not in names
        await connection.run_sync(run_upgrade, "head")

    restored = await store.get_submission(lookup=lookup_for(task))
    assert restored.clarification_parent_task_id is None


async def test_head_clarification_parent_fk_rejects_unknown_parent_and_restricts_delete(
    clean_database: AsyncEngine,
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

    child_without_parent = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="unknown-parent-child",
                idempotency_key="unknown-parent-child",
            ),
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE task_submissions "
                    "SET clarification_parent_task_id = 'unknown-parent' "
                    "WHERE task_id = :task_id"
                ),
                {"task_id": child_without_parent.task_id},
            )

    parent = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="restricted-parent",
                idempotency_key="restricted-parent",
            ),
        )
    )
    await drive_to_terminal(store, lookup_for(parent), TaskStatus.CLARIFICATION_REQUIRED)
    await store.create_clarification_child(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="restricted-child",
                idempotency_key="restricted-child",
            ),
            clarification_parent_task_id=parent.task_id,
        ),
        authenticated_channel_owner=context.actor,
    )

    with pytest.raises(sa.exc.IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM tasks WHERE task_id = :task_id"),
                {"task_id": parent.task_id},
            )


async def test_rev_0009_downgrade_requires_authorization_for_parent_links(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

    parent = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="downgrade-parent",
                idempotency_key="downgrade-parent",
            ),
        )
    )
    await drive_to_terminal(store, lookup_for(parent), TaskStatus.CLARIFICATION_REQUIRED)
    child = await store.create_clarification_child(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="downgrade-child",
                idempotency_key="downgrade-child",
            ),
            clarification_parent_task_id=parent.task_id,
        ),
        authenticated_channel_owner=context.actor,
    )
    run_upgrade, run_downgrade = alembic_runners

    with pytest.raises(MigrationSafetyError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0008_model_artifacts")
    assert exc_info.value.counts == (("task_parent_context", 1),)

    async with clean_database.begin() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        assert revision == _head_revision()
        await connection.run_sync(
            run_downgrade,
            "0008_model_artifacts",
            True,
        )
        await connection.run_sync(run_upgrade, "head")
        restored_parent = await connection.scalar(
            sa.text(
                "SELECT clarification_parent_task_id FROM task_submissions WHERE task_id = :task_id"
            ),
            {"task_id": child.task_id},
        )
    assert restored_parent is None


async def test_rev_0013_downgrade_requires_authorization_for_clarification_parent_links(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import drive_to_terminal, lookup_for, make_envelope, make_submission

    parent = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="downgrade-0013-parent",
                idempotency_key="downgrade-" + "0013-parent",
            ),
        )
    )
    await drive_to_terminal(store, lookup_for(parent), TaskStatus.CLARIFICATION_REQUIRED)
    child = await store.create_clarification_child(
        submission=make_submission(
            context,
            envelope=make_envelope(
                request_id="downgrade-0013-child",
                idempotency_key="downgrade-" + "0013-child",
            ),
            clarification_parent_task_id=parent.task_id,
        ),
        authenticated_channel_owner=context.actor,
    )
    run_upgrade, run_downgrade = alembic_runners

    with pytest.raises(MigrationSafetyError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0012_clarification_records")
    assert exc_info.value.counts == (("task_parent_context", 1),)

    async with clean_database.begin() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        assert revision == _head_revision()
        await connection.run_sync(
            run_downgrade,
            "0012_clarification_records",
            True,
        )
        await connection.run_sync(run_upgrade, "head")
        restored_parent = await connection.scalar(
            sa.text(
                "SELECT clarification_parent_task_id FROM task_submissions WHERE task_id = :task_id"
            ),
            {"task_id": child.task_id},
        )
    assert restored_parent is None


async def test_rev_0013_upgrade_rejects_legacy_parent_links(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    context: Any,
) -> None:
    run_upgrade, run_downgrade = alembic_runners

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0012_clarification_records")
        for created_seq, task_id in (
            (5013, "legacy-upgrade-parent"),
            (5014, "legacy-upgrade-child"),
        ):
            await connection.execute(
                sa.text(
                    "INSERT INTO tasks "
                    "(task_id, tenant_id, environment_id, actor, idempotency_key, "
                    "request_digest, status, version, created_seq, attempt_number, "
                    "task_failure_count, idempotency_scope_digest) VALUES "
                    "(:task_id, :tenant_id, :environment_id, :actor, :idem, :digest, "
                    ":status, 0, :created_seq, 0, 0, :scope_digest)"
                ),
                {
                    "task_id": task_id,
                    "tenant_id": context.tenant_id,
                    "environment_id": context.environment_id,
                    "actor": context.actor,
                    "idem": f"{task_id}-idem",
                    "digest": f"{created_seq:064x}",
                    "status": TaskStatus.CLARIFICATION_REQUIRED.value,
                    "created_seq": created_seq,
                    "scope_digest": f"{created_seq + 1000:064x}",
                },
            )
        for task_id, parent_task_id in (
            ("legacy-upgrade-parent", None),
            ("legacy-upgrade-child", "legacy-upgrade-parent"),
        ):
            await connection.execute(
                sa.text(
                    "INSERT INTO task_submissions "
                    "(task_id, envelope, context, as_of, submission_digest, parent_task_id) "
                    "VALUES (:task_id, '{}'::jsonb, '{}'::jsonb, now(), :digest, "
                    ":parent_task_id)"
                ),
                {
                    "task_id": task_id,
                    "digest": f"{len(task_id):064x}",
                    "parent_task_id": parent_task_id,
                },
            )
    with pytest.raises(MigrationPreconditionError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_upgrade, "head")
    assert exc_info.value.counts == (("legacy_task_parent_context", 1),)
    async with clean_database.connect() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
    assert revision == "0012_clarification_records"

    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text("UPDATE task_submissions SET parent_task_id = NULL")
        )
        await connection.run_sync(run_upgrade, "head")
        restored_revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        restored_columns = await connection.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'task_submissions'"
            )
        )
    assert restored_revision == _head_revision()
    assert "clarification_parent_task_id" in {row[0] for row in restored_columns}


async def test_rev_0011_downgrade_requires_authorization_for_v2_interactions(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO task_interaction_artifacts "
                "(task_id, artifact_version, draft, origin, provider, model, "
                "provider_origin, prompt_revision, schema_revision, input_digest, "
                "result_digest, usage, created_at, fencing_token) VALUES "
                "(:task_id, 2, CAST(:draft AS jsonb), 'model', "
                "'google-gemini-developer-api', 'gemini-3-flash-preview', "
                "'https://generativelanguage.googleapis.com', "
                "'i1-interaction-prompt-v1', 'i1-interaction-schema-v1', "
                ":input_digest, :result_digest, CAST(:usage AS jsonb), now(), 1)"
            ),
            {
                "task_id": task.task_id,
                "draft": (
                    '{"proposed_kind":"capability_request",'
                    '"capability_draft":{"intent":"starrocks.slow_query.diagnose",'
                    '"slots":{"window_minutes":"30"},"missing":[],"confidence":0.9,'
                    '"source":"model"},"confidence":0.9,"source":"model"}'
                ),
                "input_digest": "a" * 64,
                "result_digest": "b" * 64,
                "usage": '{"input_tokens":1,"output_tokens":1}',
            },
        )

    run_upgrade, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0010_local_admin_provider")
    assert exc_info.value.counts == (("task_interaction_artifacts_v2", 1),)

    async with clean_database.connect() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        remaining = await connection.scalar(
            sa.text("SELECT count(*) FROM task_interaction_artifacts")
        )
    assert revision == _head_revision()
    assert remaining == 1

    async with clean_database.begin() as connection:
        await connection.run_sync(
            run_downgrade,
            "0010_local_admin_provider",
            True,
        )
        remaining_after_down = await connection.scalar(
            sa.text("SELECT count(*) FROM task_accepted_intents")
        )
        await connection.run_sync(run_upgrade, "head")

    assert remaining_after_down == 0
    assert "task_interaction_artifacts" in await _table_names(clean_database)


async def test_rev_0012_downgrade_requires_authorization_for_clarification_records(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    store: Any,
    context: Any,
) -> None:
    from tests.conftest import make_submission

    task = await store.create_task(submission=make_submission(context))
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO task_clarification_records "
                "(task_id, record_version, subject, reason_code, missing_fields, "
                "confirmed_slots, created_at, fencing_token) VALUES "
                "(:task_id, 1, CAST(:subject AS jsonb), "
                "'interaction.kind_ambiguous', CAST(:missing_fields AS jsonb), "
                "CAST(:confirmed_slots AS jsonb), now(), 1)"
            ),
            {
                "task_id": task.task_id,
                "subject": '{"kind":"route","proposed_kind":"unknown"}',
                "missing_fields": '["time_range"]',
                "confirmed_slots": "[]",
            },
        )

    run_upgrade, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(
                run_downgrade,
                "0011_interaction_clarification",
            )
    assert exc_info.value.counts == (("task_clarification_records", 1),)

    async with clean_database.connect() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        remaining = await connection.scalar(
            sa.text("SELECT count(*) FROM task_clarification_records")
        )
    assert revision == _head_revision()
    assert remaining == 1

    async with clean_database.begin() as connection:
        await connection.run_sync(
            run_downgrade,
            "0011_interaction_clarification",
            True,
        )
    assert "task_clarification_records" not in await _table_names(clean_database)

    async with clean_database.begin() as connection:
        await connection.run_sync(run_upgrade, "head")
    assert "task_clarification_records" in await _table_names(clean_database)


async def _column_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        )
        return {row[0] for row in rows}


async def _index_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                "AND tablename = :table"
            ),
            {"table": table},
        )
        return {row[0] for row in rows}


_W1A_TABLES = {
    "user_accounts",
    "user_role_assignments",
    "external_identities",
    "admin_audit_events",
}


async def _restore_head(engine: AsyncEngine, run_upgrade: Any) -> None:
    """无条件把 schema 升回 head，并断言它真的回来了。

    ``migrated_engine`` 是 **session 级**、只升一次，``clean_database`` 只 TRUNCATE
    不重建 schema。任何主动降级的用例不还原，后续集成用例就会全部跑在旧 revision
    上，甚至在 TRUNCATE 阶段直接失败——那是测试顺序依赖，不是那些用例自己的失败。
    因此这个函数只在 ``finally`` 里调用，而且它自己带断言：悄悄失败的还原比不还原
    更难查。
    """
    async with engine.begin() as connection:
        await connection.run_sync(run_upgrade)
    async with engine.connect() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
    assert revision == _head_revision()
    assert _W1A_TABLES <= await _table_names(engine)
    assert "user_id" in await _column_names(engine, "local_admins")
    assert "web_oauth_login_contexts" in await _table_names(engine)
    assert {
        "return_intent_kind",
        "return_intent_task_id",
        "return_intent_request_id",
    } <= await _column_names(engine, "activation_requests")


async def test_rev_0017_indexes_round_trip_and_restore_head(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    expected = {
        "user_role_assignments": "ix_user_role_assignments_scope_user",
        "activation_requests": "ix_activation_requests_scope_status_requested",
        "admin_audit_events": "ix_admin_audit_events_scope_created",
    }
    try:
        for table, index in expected.items():
            assert index in await _index_names(clean_database, table)

        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0016_web_login_contexts")
        async with clean_database.connect() as connection:
            assert await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == "0016_web_login_contexts"
        for table, index in expected.items():
            assert index not in await _index_names(clean_database, table)

        async with clean_database.begin() as connection:
            await connection.run_sync(run_upgrade, "head")
        for table, index in expected.items():
            assert index in await _index_names(clean_database, table)
    finally:
        await _restore_head(clean_database, run_upgrade)


async def _seed_directory_and_audit(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO user_accounts "
                "(user_id, actor, display_name, status, created_at, updated_at) "
                "VALUES ('u-1', 'alice', 'Alice', 'active', now(), now())"
            )
        )
        await connection.execute(
            sa.text(
                "INSERT INTO user_role_assignments "
                "(user_id, tenant_id, environment_id, role, created_by, "
                "created_at, updated_at) "
                "VALUES ('u-1', 't-1', 'dev', 'operator', 'admin-1', now(), now())"
            )
        )
        await connection.execute(
            sa.text(
                "INSERT INTO admin_audit_events "
                "(event_id, operation_id, tenant_id, environment_id, actor_user_id, "
                "actor, auth_source, action, target_kind, target_ref_digest, outcome, "
                "reason_code, effect_role, effect_status, created_at) VALUES "
                "('e-1', 'op-1', 't-1', 'dev', 'admin-1', 'admin', 'local_admin', "
                "'role_assigned', 'user', :digest, 'succeeded', NULL, 'operator', "
                "NULL, now())"
            ),
            {"digest": "a" * 64},
        )


async def test_rev_0014_downgrade_requires_authorization_for_identity_and_audit(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    """有授权事实或审计事件时默认拒绝降级；显式授权后可完整往返。

    降级会**同时**丢掉两类不可再生的东西：当前谁有什么权限，以及这些权限是怎么
    给出去的。后者尤其无法从别处重建——审计是 append-only 的唯一记录。
    """
    run_upgrade, run_downgrade = alembic_runners
    try:
        await _seed_directory_and_audit(clean_database)

        with pytest.raises(MigrationSafetyError) as exc_info:
            async with clean_database.begin() as connection:
                await connection.run_sync(run_downgrade, "0013_clarification_parent")

        # 分类精确到"哪一类数据会被丢掉"，不是一个布尔。手工预检会把它降级成布尔，
        # 而 counts 正是既有用例断言的那个值。
        assert exc_info.value.counts == (
            ("identity_directory", 1),
            ("admin_audit", 1),
        )

        async with clean_database.connect() as connection:
            revision = await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
            accounts = await connection.scalar(
                sa.text("SELECT count(*) FROM user_accounts")
            )
            assignments = await connection.scalar(
                sa.text("SELECT count(*) FROM user_role_assignments")
            )
            events = await connection.scalar(
                sa.text("SELECT count(*) FROM admin_audit_events")
            )
        assert revision == _head_revision()
        assert (accounts, assignments, events) == (1, 1, 1)

        async with clean_database.begin() as connection:
            await connection.run_sync(
                run_downgrade, "0013_clarification_parent", True
            )
        assert not _W1A_TABLES & await _table_names(clean_database)
        assert "user_id" not in await _column_names(clean_database, "local_admins")
    finally:
        await _restore_head(clean_database, run_upgrade)


async def test_rev_0014_downgrade_needs_no_authorization_on_an_empty_directory(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    """正常对照：没有受保护数据时不该要授权。

    只断"有数据会被拒"时，一条永远抛异常的守卫也照样绿——而那条守卫会让任何一次
    合法回滚都变成需要人工干预的死局。
    """
    run_upgrade, run_downgrade = alembic_runners
    try:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0013_clarification_parent")
        assert not _W1A_TABLES & await _table_names(clean_database)
    finally:
        await _restore_head(clean_database, run_upgrade)


async def _seed_activation_request(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO activation_requests "
                "(request_id, tenant_id, environment_id, provider, subject_ref, "
                "subject_ref_digest, source, source_event_digest, source_chat_digest, "
                "return_intent_kind, return_intent_task_id, "
                "return_intent_request_id, requested_at, expires_at, status, "
                "decided_at, decided_by, "
                "approved_role) VALUES "
                "('activation-1', 't-1', 'dev', 'feishu', 'ou_subject', :digest, "
                "'web_login', NULL, NULL, 'workbench', NULL, NULL, "
                "now(), now() + interval '1 day', "
                "'pending', NULL, NULL, NULL)"
            ),
            {"digest": "b" * 64},
        )


async def test_rev_0015_downgrade_needs_no_authorization_when_empty(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    """正常对照：空表能降级，避免把安全门写成无条件拒绝。"""
    run_upgrade, run_downgrade = alembic_runners
    try:
        async with clean_database.begin() as connection:
            await connection.run_sync(
                run_downgrade, "0014_identity_admin_audit"
            )
        assert "activation_requests" not in await _table_names(clean_database)
    finally:
        await _restore_head(clean_database, run_upgrade)
    assert "activation_requests" in await _table_names(clean_database)


async def test_rev_0015_downgrade_guards_requests_with_exact_counts(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    try:
        await _seed_activation_request(clean_database)
        with pytest.raises(MigrationSafetyError) as exc_info:
            async with clean_database.begin() as connection:
                await connection.run_sync(
                    run_downgrade, "0014_identity_admin_audit"
                )
        assert exc_info.value.counts == (
            ("activation_requests", 1),
            ("activation_audit", 0),
        )
        async with clean_database.connect() as connection:
            assert await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == _head_revision()
            assert await connection.scalar(
                sa.text("SELECT count(*) FROM activation_requests")
            ) == 1

        async with clean_database.begin() as connection:
            await connection.run_sync(
                run_downgrade, "0014_identity_admin_audit", True
            )
        assert "activation_requests" not in await _table_names(clean_database)
    finally:
        await _restore_head(clean_database, run_upgrade)
    assert "activation_requests" in await _table_names(clean_database)


async def test_rev_0015_never_discards_activation_audit_even_when_authorized(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    try:
        await _insert_audit_event(
            clean_database,
            action="activation_rejected",
            target_kind="activation",
            outcome="denied",
            reason_code="conflict",
            effect_role=None,
        )
        with pytest.raises(MigrationSafetyError) as exc_info:
            async with clean_database.begin() as connection:
                await connection.run_sync(
                    run_downgrade, "0014_identity_admin_audit", True
                )
        assert exc_info.value.counts == (
            ("activation_requests", 0),
            ("activation_audit", 1),
        )
        async with clean_database.connect() as connection:
            assert await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == _head_revision()
            assert await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM admin_audit_events "
                    "WHERE action = 'activation_rejected'"
                )
            ) == 1
    finally:
        await _restore_head(clean_database, run_upgrade)
    assert "activation_requests" in await _table_names(clean_database)


async def test_rev_0016_upgrade_invalidates_old_states_and_backfills_intents(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    try:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0015_activation_requests")
            await connection.execute(
                sa.text(
                    "INSERT INTO web_oauth_states "
                    "(state_digest, issued_at, expires_at, consumed_at) VALUES "
                    "(:digest, now(), now() + interval '10 minutes', NULL)"
                ),
                {"digest": "c" * 64},
            )
            for request_id, source, event_digest, chat_digest in (
                ("old-web", "web_login", None, None),
                ("old-group", "feishu_group", "d" * 64, "e" * 64),
            ):
                await connection.execute(
                    sa.text(
                        "INSERT INTO activation_requests "
                        "(request_id, tenant_id, environment_id, provider, "
                        "subject_ref, subject_ref_digest, source, "
                        "source_event_digest, source_chat_digest, requested_at, "
                        "expires_at, status, decided_at, decided_by, approved_role) "
                        "VALUES (:request_id, 't-1', 'dev', 'feishu', :subject, "
                        ":subject_digest, :source, :event_digest, :chat_digest, "
                        "now(), now() + interval '1 day', 'pending', NULL, NULL, NULL)"
                    ),
                    {
                        "request_id": request_id,
                        "subject": f"ou_{request_id}",
                        "subject_digest": (
                            "f" if request_id == "old-web" else "1"
                        )
                        * 64,
                        "source": source,
                        "event_digest": event_digest,
                        "chat_digest": chat_digest,
                    },
                )
            await connection.run_sync(run_upgrade, "head")

        async with clean_database.connect() as connection:
            state_count = await connection.scalar(
                sa.text("SELECT count(*) FROM web_oauth_states")
            )
            context_count = await connection.scalar(
                sa.text("SELECT count(*) FROM web_oauth_login_contexts")
            )
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT request_id, return_intent_kind FROM "
                        "activation_requests ORDER BY request_id"
                    )
                )
            ).all()
        assert state_count == 0
        assert context_count == 0
        assert rows == [("old-group", None), ("old-web", "workbench")]
    finally:
        await _restore_head(clean_database, run_upgrade)


async def test_rev_0016_downgrade_rejects_each_w2_only_fact_with_exact_counts(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    try:
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO web_oauth_states "
                    "(state_digest, issued_at, expires_at, consumed_at) VALUES "
                    "(:digest, now(), now() + interval '10 minutes', NULL)"
                ),
                {"digest": "2" * 64},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO web_oauth_login_contexts "
                    "(state_digest, return_intent_kind, return_intent_task_id, "
                    "return_intent_request_id) VALUES (:digest, 'workbench', NULL, NULL)"
                ),
                {"digest": "2" * 64},
            )
            for request_id, source, kind, task_id in (
                ("deep-link", "safe_task_link", "safe_task_detail", "task-1"),
                ("admin-login", "web_login", "admin_center", None),
            ):
                await connection.execute(
                    sa.text(
                        "INSERT INTO activation_requests "
                        "(request_id, tenant_id, environment_id, provider, "
                        "subject_ref, subject_ref_digest, source, "
                        "return_intent_kind, return_intent_task_id, "
                        "return_intent_request_id, source_event_digest, "
                        "source_chat_digest, requested_at, expires_at, status, "
                        "decided_at, decided_by, approved_role) VALUES "
                        "(:request_id, 't-1', 'dev', 'feishu', :subject, "
                        ":subject_digest, :source, :kind, :task_id, NULL, NULL, NULL, "
                        "now(), now() + interval '1 day', 'pending', NULL, NULL, NULL)"
                    ),
                    {
                        "request_id": request_id,
                        "subject": f"ou_{request_id}",
                        "subject_digest": (
                            "3" if request_id == "deep-link" else "4"
                        )
                        * 64,
                        "source": source,
                        "kind": kind,
                        "task_id": task_id,
                    },
                )

        with pytest.raises(MigrationSafetyError) as exc_info:
            async with clean_database.begin() as connection:
                await connection.run_sync(
                    run_downgrade, "0015_activation_requests", True
                )
        assert exc_info.value.counts == (
            ("active_login_context", 1),
            ("safe_task_link_activation", 1),
            ("non_workbench_web_activation", 1),
        )
        async with clean_database.connect() as connection:
            assert await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == _head_revision()
    finally:
        await _restore_head(clean_database, run_upgrade)


async def test_rev_0016_downgrade_preserves_every_w1b_activation_shape(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    try:
        await _seed_activation_request(clean_database)
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0015_activation_requests")
        assert "web_oauth_login_contexts" not in await _table_names(clean_database)
        assert "return_intent_kind" not in await _column_names(
            clean_database, "activation_requests"
        )
        async with clean_database.connect() as connection:
            assert await connection.scalar(
                sa.text("SELECT count(*) FROM activation_requests")
            ) == 1
    finally:
        await _restore_head(clean_database, run_upgrade)


async def _insert_audit_event(engine: AsyncEngine, **values: Any) -> None:
    row = {
        "event_id": "e-1",
        "operation_id": "op-1",
        "tenant_id": "t-1",
        "environment_id": "dev",
        "actor_user_id": "admin-1",
        "actor": "admin",
        "auth_source": "local_admin",
        "action": "role_assigned",
        "target_kind": "user",
        "target_ref_digest": "a" * 64,
        "outcome": "succeeded",
        "reason_code": None,
        "effect_role": "operator",
        "effect_status": None,
    }
    row.update(values)
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO admin_audit_events "
                "(event_id, operation_id, tenant_id, environment_id, actor_user_id, "
                "actor, auth_source, action, target_kind, target_ref_digest, outcome, "
                "reason_code, effect_role, effect_status, created_at) VALUES "
                "(:event_id, :operation_id, :tenant_id, :environment_id, "
                ":actor_user_id, :actor, :auth_source, :action, :target_kind, "
                ":target_ref_digest, :outcome, :reason_code, :effect_role, "
                ":effect_status, now())"
            ),
            row,
        )


async def test_the_audit_table_itself_refuses_a_forged_success(
    clean_database: AsyncEngine,
) -> None:
    """三条 CHECK 由**数据库**执行，不只是在 metadata 里声明过。

    结构用例只能证明"约束声明了"；只有真的 INSERT 一行才能证明"迁移把它建出来了"。
    这两件事在漂移时恰好分开：``schema.py`` 有而迁移漏掉，所有内存用例照常全绿，
    而生产库上那一行会写进去。

    这里逐条恢复的是三个具体的旧缺陷：
    1. 一次被拒的操作也能声称它授予了角色；
    2. 改了角色却不记是哪个角色（后续改动一覆盖就再也还原不出来）；
    3. 目录动作写出一条 ``STARTED``，声称有一个本不存在的"结果未知"中间态。
    """
    await _insert_audit_event(clean_database)

    with pytest.raises(sa.exc.IntegrityError):
        await _insert_audit_event(
            clean_database,
            event_id="e-2",
            operation_id="op-2",
            outcome="denied",
            reason_code="actor_not_admin",
            effect_role="admin",
        )

    with pytest.raises(sa.exc.IntegrityError):
        await _insert_audit_event(
            clean_database, event_id="e-3", operation_id="op-3", effect_role=None
        )

    with pytest.raises(sa.exc.IntegrityError):
        await _insert_audit_event(
            clean_database,
            event_id="e-4",
            operation_id="op-4",
            outcome="started",
            effect_role=None,
        )

    async with clean_database.connect() as connection:
        remaining = await connection.scalar(
            sa.text("SELECT count(*) FROM admin_audit_events")
        )
    assert remaining == 1


async def test_one_operation_cannot_hold_two_events_at_the_same_stage(
    clean_database: AsyncEngine,
) -> None:
    """终态偏唯一索引在真实库上生效，而它**不是** ``operation_id`` 的全局唯一。

    正对照是最后那次插入：换一个 ``operation_id`` 写同一阶段必须成功。只断"重复
    终态被拒"时，给 ``operation_id`` 加一条全局 UNIQUE 也照样绿——而全局唯一会堵死
    规格 §14.2 的两阶段配置审计。

    **另一半（同一个 operation 同时有 STARTED 和终态）在 W1a 无法构造**，因为
    ``DIRECTORY_ACTIONS`` 恰好等于本阶段全部八个 action，单阶段 CHECK 会拒绝任何
    ``STARTED`` 行。这是设计结果不是遗漏：写这条用例时第一版正是拿一个目录动作去
    插 ``STARTED``，被 CHECK 拒了——当时要么改测试，要么放宽那条 CHECK，而放宽它
    等于允许目录动作声称一个本不存在的"结果未知"中间态。这里改的是测试。
    W4a 引入第一个非目录动作时，那一半才有得测。
    """
    await _insert_audit_event(clean_database)

    with pytest.raises(sa.exc.IntegrityError):
        await _insert_audit_event(
            clean_database,
            event_id="e-2",
            outcome="failed",
            reason_code="conflict",
            effect_role=None,
        )

    await _insert_audit_event(clean_database, event_id="e-3", operation_id="op-2")
    async with clean_database.connect() as connection:
        remaining = await connection.scalar(
            sa.text("SELECT count(*) FROM admin_audit_events")
        )
    assert remaining == 2


async def test_no_w1a_action_can_write_a_started_row(
    clean_database: AsyncEngine,
) -> None:
    """W1a 的每一个目录 action 都写不出 ``STARTED``，逐个动作断言。

    只测一个动作时，单阶段 CHECK 的动作列表漏掉某一个不会被发现——而漏掉的那个
    动作从此可以在数据库层留下一条"结果未知"的事件。W4a 的三个配置动作是唯一
    合法的两阶段动作，见下一条用例；这里遍历的是它的补集。
    """
    from xiaowei_agent.contracts.admin_audit import DIRECTORY_ACTIONS, STARTABLE_ACTIONS
    from xiaowei_agent.contracts.enums import AdminAuditAction

    assert DIRECTORY_ACTIONS == frozenset(AdminAuditAction) - STARTABLE_ACTIONS
    for index, action in enumerate(sorted(DIRECTORY_ACTIONS)):
        with pytest.raises(sa.exc.IntegrityError):
            await _insert_audit_event(
                clean_database,
                event_id=f"e-{index}",
                operation_id=f"op-{index}",
                action=action.value,
                outcome="started",
                effect_role=None,
            )

    async with clean_database.connect() as connection:
        remaining = await connection.scalar(
            sa.text("SELECT count(*) FROM admin_audit_events")
        )
    assert remaining == 0


async def test_w4a_config_actions_write_exactly_one_started_row_per_operation(
    clean_database: AsyncEngine,
) -> None:
    """对照：rev_0018 放行三个配置动作的 ``STARTED``，但同一 operation 只能一条。"""
    from xiaowei_agent.contracts.admin_audit import STARTABLE_ACTIONS

    for index, action in enumerate(sorted(STARTABLE_ACTIONS)):
        values = {
            "operation_id": f"w4:{index:032x}",
            "action": action.value,
            "target_kind": "config",
            "outcome": "started",
            "effect_role": None,
        }
        await _insert_audit_event(clean_database, event_id=f"e-{index}", **values)
        with pytest.raises(sa.exc.IntegrityError):
            await _insert_audit_event(
                clean_database, event_id=f"e-{index}-again", **values
            )

    async with clean_database.connect() as connection:
        started = await connection.scalar(
            sa.text("SELECT count(*) FROM admin_audit_events WHERE outcome = 'started'")
        )
    assert started == len(STARTABLE_ACTIONS)

"""两条 upgrade 路径都要验证（DEVELOPMENT_PLAN §7 M4 测试门明文）。

空库能建起来只证明了一半：真正会出问题的是**已有数据**的库——一条加了 NOT NULL
但没给默认值的列、一条与既有行冲突的 CHECK，都只在有数据时才炸，而那正是生产
环境的形态。
"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.persistence.schema import ALL_TABLES, FENCING_SEQUENCE_NAME


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
        assert FENCING_SEQUENCE_NAME in {row[0] for row in rows}


async def test_downgrade_then_upgrade_over_a_database_with_data(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any], store: Any, context: Any
) -> None:
    """有数据的库：先写入真实数据，再 downgrade 到底，再 upgrade 回来。

    ``downgrade`` 会连数据一起删除——这是**预期**行为，本条断言的是"回滚不会卡在
    残留对象上"，不是"数据能存活"。残留的表会让下一次 upgrade 在"已存在"上失败，
    把一次可回滚的迁移变成需要人工清理的死局。
    """
    from tests.conftest import make_envelope

    await store.create_task(envelope=make_envelope(), context=context)
    async with clean_database.connect() as connection:
        count = await connection.execute(sa.text("SELECT count(*) FROM tasks"))
        assert count.scalar_one() == 1

    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade)
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
    from tests.conftest import make_envelope

    record = await store.create_task(envelope=make_envelope(), context=context)
    with pytest.raises(sa.exc.IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text("UPDATE tasks SET lease_owner = 'w1' WHERE task_id = :task_id"),
                {"task_id": record.task_id},
            )

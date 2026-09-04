"""迁移与 ``schema.py`` 必须描述同一套表 —— **离线比对，不需要数据库**。

M4 有意让 schema 出现在两处：``persistence/schema.py`` 是活的（``PostgresTaskStore``
用它构造语句），迁移是冻结的历史快照（回放它必须建出**当初**那套表，而不是今天的）。
两者都必要，因此漂移是必然风险，不是可以靠纪律避免的偶发错误。

漂移的后果不是报错而是**沉默的错位**：迁移建出的表少一个 CHECK，而代码以为它在，
于是"数据库层独立表达同一不变量"这条对价直接落空，且所有内存用例照常全绿。

本文件用 Alembic 的**离线模式**（``upgrade --sql``）拿到迁移真正会发出的 DDL，再把
``schema.py`` 的每张表编译成同一方言的 DDL，逐表比较。离线模式不连数据库，因此这条
检查在默认测试路径上就能跑——不必等到有 PostgreSQL 才发现两边不一致。
"""

import contextlib
import io
import re
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from xiaowei_agent.persistence.schema import ALL_TABLES, FENCING_SEQUENCE_NAME

_ROOT = Path(__file__).resolve().parents[2]

# 迁移自己的记账表，不属于业务 schema，比较时排除。
_ALEMBIC_BOOKKEEPING = "alembic_version"


def _offline_upgrade_sql() -> str:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / config.get_main_option(
        "script_location", ""
    )))
    # 离线模式不建立连接，URL 只用于选方言。此处必须是一个**不可路由的占位**，
    # 而不是任何真实实例——离线模式不连它，但把真实地址写进测试等于把连接串提交
    # 进仓库。
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def _normalise(statement: str) -> str:
    """折叠空白与括号旁的空格，去掉尾分号——只比较结构，不比较排版。

    括号旁的空格纯属排版：Alembic 与 ``CreateTable`` 的换行位置不同，但那不是漂移。
    折叠范围**只限空白**，任何标识符、类型、约束文本的差异都会保留下来。
    """
    collapsed = re.sub(r"\s+", " ", statement).strip().rstrip(";").strip()
    return collapsed.replace("( ", "(").replace(" )", ")")


def _create_table_statements(sql: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.DOTALL):
        name = match.group(1)
        if name != _ALEMBIC_BOOKKEEPING:
            found[name] = _normalise(f"CREATE TABLE {name} ({match.group(2)})")
    return found


def test_migration_creates_exactly_the_tables_declared_in_schema() -> None:
    """表集合相等。少一张会让代码在运行时才发现表不存在，多一张则无人维护。"""
    assert set(_create_table_statements(_offline_upgrade_sql())) == {
        table.name for table in ALL_TABLES
    }


def test_each_table_ddl_matches_the_schema_module() -> None:
    """逐表比较完整 DDL：列、类型、可空性、主键、唯一约束、CHECK 全部在内。

    比 "表名对得上" 严格得多——漂移几乎总是发生在某一条约束上，而不是整张表上。
    """
    emitted = _create_table_statements(_offline_upgrade_sql())
    dialect = postgresql.dialect()
    for table in ALL_TABLES:
        expected = _normalise(str(CreateTable(table).compile(dialect=dialect)))
        assert emitted[table.name] == expected, f"{table.name} 的迁移 DDL 与 schema.py 不一致"


def test_migration_creates_the_fencing_sequence() -> None:
    """fencing token 的单调性由序列提供；序列没建出来，acquire_lease 会在运行时才炸。"""
    assert f"CREATE SEQUENCE {FENCING_SEQUENCE_NAME}" in _offline_upgrade_sql()


def test_downgrade_drops_everything_upgrade_created() -> None:
    """``downgrade()`` 必须能把库还原干净。

    残留的表会让下一次 ``upgrade`` 在"已存在"上失败，把一次可回滚的迁移变成需要
    人工清理的死局。
    """
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / config.get_main_option(
        "script_location", ""
    )))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.downgrade(config, "0001_initial:base", sql=True)
    emitted = buffer.getvalue()
    for table in ALL_TABLES:
        assert f"DROP TABLE {table.name}" in emitted
    assert f"DROP SEQUENCE {FENCING_SEQUENCE_NAME}" in emitted


def test_task_id_columns_are_text_not_uuid() -> None:
    """``TaskRecord.task_id`` 是 ``StrictStr``；``uuid`` 列读回的是 ``UUID`` 对象。

    strict 模式会直接拒绝那个对象。写 DDL 时"主键当然用 uuid"的直觉在这里恰好是
    错的，因此钉死。
    """
    for table in ALL_TABLES:
        column = table.c["task_id"]
        assert isinstance(column.type, sa.Text), f"{table.name}.task_id 必须是 text"

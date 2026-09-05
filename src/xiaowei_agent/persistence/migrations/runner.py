"""程序化 Alembic runner；integration 与应用迁移入口共用。"""

import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex

_MIGRATIONS_DIR = Path(__file__).resolve().parent


class MigrationCompatibilityError(RuntimeError):
    """当前数据无法安全恢复 M4 幂等唯一约束。"""


def _probe_m4_index_compatibility(connection: Connection) -> None:
    """在 SAVEPOINT 中证明旧三列唯一索引可建立，不留下任何 DDL。"""
    tasks = sa.Table(
        "tasks",
        sa.MetaData(),
        sa.Column("tenant_id", sa.Text),
        sa.Column("environment_id", sa.Text),
        sa.Column("idempotency_key", sa.Text),
    )
    index = sa.Index(
        f"ix_m4_downgrade_probe_{uuid.uuid4().hex}",
        tasks.c.tenant_id,
        tasks.c.environment_id,
        tasks.c.idempotency_key,
        unique=True,
    )
    nested = connection.begin_nested()
    failed = False
    try:
        connection.execute(CreateIndex(index))
    except sa.exc.DatabaseError:
        failed = True
    finally:
        nested.rollback()
    if failed:
        raise MigrationCompatibilityError("M4_DOWNGRADE_INCOMPATIBLE")


def alembic_config() -> Config:
    ini = Path.cwd() / "alembic.ini"
    config = Config(str(ini)) if ini.is_file() else Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return config


def run_upgrade(connection: Connection, revision: str = "head") -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def run_downgrade(
    connection: Connection, revision: str = "base", allow_destructive: bool = False
) -> None:
    if revision in {"base", "0001_initial"}:
        _probe_m4_index_compatibility(connection)
    config = alembic_config()
    config.attributes["connection"] = connection
    config.attributes["allow_destructive"] = allow_destructive
    command.downgrade(config, revision)

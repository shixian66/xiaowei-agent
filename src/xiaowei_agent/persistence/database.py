"""PostgreSQL Engine 与 readiness 的唯一构造入口。"""

from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from xiaowei_agent.contracts import ReadinessReport
from xiaowei_agent.persistence.migrations.runner import alembic_config

_PASSWORD_FILE_LIMIT = 4096


class DatabaseSettings(Protocol):
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_user: str
    postgres_password_file: str
    db_connect_timeout_seconds: float
    db_command_timeout_seconds: float
    db_pool_size: int
    db_pool_max_overflow: int


class DatabaseConfigurationError(RuntimeError):
    """数据库配置不可用；消息不携带路径或 secret。"""


def read_postgres_password(settings: DatabaseSettings) -> str:
    """读取有界单行 secret；失败只报告配置字段与类别。"""
    path = Path(settings.postgres_password_file)
    failed = not path.is_file()
    raw = ""
    if not failed:
        try:
            with path.open(encoding="utf-8") as stream:
                raw = stream.read(_PASSWORD_FILE_LIMIT + 1)
        except (OSError, UnicodeError):
            failed = True
    password = raw.rstrip("\r\n")
    if (
        failed
        or not password
        or len(raw) > _PASSWORD_FILE_LIMIT
        or "\n" in password
        or "\r" in password
        or "\x00" in password
    ):
        raise DatabaseConfigurationError(
            "database configuration invalid: postgres_password_file"
        )
    return password


def database_url(settings: DatabaseSettings) -> URL:
    """以结构化 URL 组装凭证，避免把可打印 DSN 当作配置传播。"""
    return URL.create(
        drivername="postgresql+asyncpg",
        username=settings.postgres_user,
        password=read_postgres_password(settings),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_database,
    )


def create_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    """供 API、Worker、readiness 与 migrate 共用的 Engine factory。"""
    return create_async_engine(
        database_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            "command_timeout": settings.db_command_timeout_seconds,
        },
    )


def _current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


class PostgresReadinessProbe:
    """只验证 DB ping、migration head 与装配完整性，不触碰任务。"""

    def __init__(self, *, engine: AsyncEngine, assembled: bool) -> None:
        self._engine = engine
        self._assembled = assembled

    async def check(self) -> ReadinessReport:
        database_ok = False
        revision_matches_head = False
        try:
            async with self._engine.connect() as connection:
                await connection.execute(sa.text("SELECT 1"))
                current = await connection.run_sync(_current_revision)
            head = ScriptDirectory.from_config(alembic_config()).get_current_head()
            database_ok = True
            revision_matches_head = current == head
        except Exception:
            database_ok = False
            revision_matches_head = False
        return ReadinessReport(
            database_ok=database_ok,
            revision_matches_head=revision_matches_head,
            assembled=self._assembled,
        )

"""使用应用 Settings 与共享 AsyncEngine 执行 Alembic migration。"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.persistence.database import (
    DatabaseConfigurationError,
    create_database_engine,
)
from xiaowei_agent.persistence.migrations.runner import run_downgrade, run_upgrade

EngineFactory = Callable[[Settings], AsyncEngine]


async def execute_migration(
    *,
    settings: Settings,
    revision: str,
    downgrade: bool,
    allow_destructive: bool,
    engine_factory: EngineFactory = create_database_engine,
) -> None:
    """经同一 async→sync 桥执行 migration，并始终释放 Engine。"""
    engine = engine_factory(settings)
    try:
        async with engine.begin() as connection:
            if downgrade:
                await connection.run_sync(
                    run_downgrade, revision, allow_destructive
                )
            else:
                await connection.run_sync(run_upgrade, revision)
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaowei-migrate")
    parser.add_argument("--downgrade", action="store_true")
    parser.add_argument("--revision", default="head")
    parser.add_argument("--allow-destructive", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, *, stderr: TextIO = sys.stderr) -> int:
    try:
        args = _parser().parse_args(argv)
        settings = load_settings()
        asyncio.run(
            execute_migration(
                settings=settings,
                revision=args.revision,
                downgrade=args.downgrade,
                allow_destructive=args.allow_destructive,
            )
        )
    except (ConfigError, DatabaseConfigurationError):
        stderr.write("xiaowei-migrate: configuration_error\n")
        return 2
    except Exception:
        stderr.write("xiaowei-migrate: migration_failed\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

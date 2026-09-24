"""全库终态激活保留的一次性命令（W5 §2.1）。

``python -m xiaowei_agent.interfaces.activation_retention``

- **不接受任何参数**：保留期是代码常量，范围是全库所有作用域。可传 cutoff、作用域或
  request id 就等于让调用方决定删哪些行。
- stdout 只有一行四个计数的 JSON；失败只在 stderr 写一个闭集错误码。部署证据会被
  复制进工单，因此 request id、主体、决定人、作用域与异常正文一律不输出。
- 无论成功或失败，engine 都在退出前释放。
"""

import asyncio
import datetime as dt
import json
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.contracts.activation import ActivationRetentionReport
from xiaowei_agent.persistence.activation import ActivationRetentionStore
from xiaowei_agent.persistence.database import (
    DatabaseConfigurationError,
    create_database_engine,
)
from xiaowei_agent.persistence.errors import PersistenceUnavailableError
from xiaowei_agent.persistence.postgres import PostgresActivationRetentionStore

Clock = Callable[[], dt.datetime]
EngineFactory = Callable[[Settings], AsyncEngine]
StoreFactory = Callable[[AsyncEngine, Clock], ActivationRetentionStore]

_PREFIX = "activation-retention"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _postgres_store(engine: AsyncEngine, clock: Clock) -> ActivationRetentionStore:
    return PostgresActivationRetentionStore(engine=engine, clock=clock)


async def _purge(
    *,
    settings: Settings,
    engine_factory: EngineFactory,
    store_factory: StoreFactory,
    clock: Clock,
) -> ActivationRetentionReport:
    engine = engine_factory(settings)
    try:
        return await store_factory(engine, clock).purge_expired_terminal()
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    engine_factory: EngineFactory = create_database_engine,
    store_factory: StoreFactory = _postgres_store,
    clock: Clock = _utc_now,
) -> int:
    """执行一次全库清理；0 成功，1 运行失败，2 调用或配置错误。"""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments:
        stderr.write(f"{_PREFIX}: usage_invalid\n")
        return 2
    try:
        settings = load_settings()
    except ConfigError:
        stderr.write(f"{_PREFIX}: configuration_error\n")
        return 2
    code: str | None = None
    report: ActivationRetentionReport | None = None
    try:
        report = asyncio.run(
            _purge(
                settings=settings,
                engine_factory=engine_factory,
                store_factory=store_factory,
                clock=clock,
            )
        )
    except DatabaseConfigurationError:
        code = "configuration_error"
    except PersistenceUnavailableError:
        code = "database_unavailable"
    except Exception:
        code = "retention_failed"
    if code is not None or report is None:
        stderr.write(f"{_PREFIX}: {code or 'retention_failed'}\n")
        return 2 if code == "configuration_error" else 1
    stdout.write(json.dumps(report.model_dump(), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由运维一次性调用
    sys.exit(main())


__all__ = ["main"]

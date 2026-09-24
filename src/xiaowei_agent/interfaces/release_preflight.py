"""W5 首次 release 发布预检（只读）。

``python -m xiaowei_agent.interfaces.release_preflight``

切换到 ``release`` 运行形态之前按固定顺序回答四件事，任一不满足即 fail-closed：

1. 三域配置目录可用且旧 ``integrations.json`` 已迁走（复用 :mod:`config_preflight`）；
2. PostgreSQL 可达且 schema 在 migration head；
3. 固定 ``dev-local/dev`` 作用域里没有非终态任务（``tasks_not_drained``）——release 不
   跨运行形态接管运行中的任务；
4. 该作用域里没有任何已持久化 plan 或 evidence 的任务
   （``historical_execution_data_present``）。既有 schema 无法证明它们出自哪个运行形态，
   所以已成功的也不继承；命中后只能换新数据库或另行批准数据处置。

预检**不写数据库、不删历史、不接受任何参数**（没有 ``--force``、忽略标志或作用域），
输出只有一行闭集结果与计数，绝不打印 task id、正文或 actor。网络面只有目标 PostgreSQL。
"""

import asyncio
import datetime as dt
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TextIO

from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ReadinessProbe,
    ScopeTaskPageQuery,
)
from xiaowei_agent.contracts.identity import (
    LOCAL_ADMIN_ENVIRONMENT_ID,
    LOCAL_ADMIN_TENANT_ID,
)
from xiaowei_agent.interfaces.config_preflight import PREFLIGHT_OK, run_preflight
from xiaowei_agent.interfaces.integration_config_file import CONFIG_ROOT
from xiaowei_agent.persistence.database import (
    DatabaseConfigurationError,
    PostgresReadinessProbe,
    create_database_engine,
)
from xiaowei_agent.persistence.evidence import EvidenceLedger
from xiaowei_agent.persistence.plans import PlanNotFoundError, PlanStore
from xiaowei_agent.persistence.store import TaskStore

_COMMAND: Final[str] = "release-preflight"
_PAGE_LIMIT: Final[int] = 100
_CONFIG_PREFIX: Final[str] = "preflight: "

OK: Final[str] = "ok"
TASKS_NOT_DRAINED: Final[str] = "tasks_not_drained"
HISTORICAL_EXECUTION_DATA_PRESENT: Final[str] = "historical_execution_data_present"
DATABASE_UNAVAILABLE: Final[str] = "database_unavailable"
SCHEMA_NOT_AT_HEAD: Final[str] = "schema_not_at_head"

Clock = Callable[[], dt.datetime]
EngineFactory = Callable[[Settings], AsyncEngine]


@dataclass(frozen=True)
class PreflightStores:
    """预检只需要的四个只读端口；不含任何写入口。"""

    task_store: TaskStore
    plan_store: PlanStore
    ledger: EvidenceLedger
    readiness: ReadinessProbe


StoresFactory = Callable[[AsyncEngine, Clock], PreflightStores]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _postgres_stores(engine: AsyncEngine, clock: Clock) -> PreflightStores:
    from xiaowei_agent.persistence.postgres import (
        PostgresEvidenceLedger,
        PostgresPlanStore,
        PostgresTaskStore,
    )

    return PreflightStores(
        task_store=PostgresTaskStore(engine=engine, clock=clock),
        plan_store=PostgresPlanStore(engine=engine),
        ledger=PostgresEvidenceLedger(engine=engine),
        readiness=PostgresReadinessProbe(engine=engine, assembled=True),
    )


async def _has_execution_data(stores: PreflightStores, task_id: str) -> bool:
    try:
        await stores.plan_store.load(task_id=task_id)
    except PlanNotFoundError:
        return bool(await stores.ledger.load(task_id=task_id))
    return True


async def _scan_history(stores: PreflightStores) -> dict[str, object]:
    """完整分页扫描固定作用域；只累计计数，不保留任何 task id。"""
    total = 0
    non_terminal = 0
    with_execution_data = 0
    cursor: int | None = None
    while True:
        page = await stores.task_store.list_tasks_for_scope(
            query=ScopeTaskPageQuery(
                tenant_id=LOCAL_ADMIN_TENANT_ID,
                environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
                before_created_seq=cursor,
                limit=_PAGE_LIMIT,
            )
        )
        for item in page.items:
            total += 1
            if item.record.status not in TERMINAL_STATUSES:
                non_terminal += 1
            if await _has_execution_data(stores, item.record.task_id):
                with_execution_data += 1
        if page.next_created_seq is None:
            break
        cursor = page.next_created_seq
    result = OK
    if non_terminal:
        # 先排空再谈历史：运行中的任务会在扫描之后继续产出 plan/evidence。
        result = TASKS_NOT_DRAINED
    elif with_execution_data:
        result = HISTORICAL_EXECUTION_DATA_PRESENT
    return {
        "result": result,
        "tasks_total": total,
        "non_terminal_tasks": non_terminal,
        "tasks_with_execution_data": with_execution_data,
    }


async def run_release_preflight(
    *,
    settings: Settings,
    engine_factory: EngineFactory = create_database_engine,
    stores_factory: StoresFactory = _postgres_stores,
    clock: Clock = _utc_now,
    config_root: str = CONFIG_ROOT,
) -> dict[str, object]:
    """按固定顺序执行全部检查，返回一行闭集结果；数据库异常原样上抛给 ``main``。"""
    config_line = run_preflight(config_root)
    if config_line != PREFLIGHT_OK:
        return {"result": "config_" + config_line.removeprefix(_CONFIG_PREFIX)}
    engine = engine_factory(settings)
    try:
        stores = stores_factory(engine, clock)
        readiness = await stores.readiness.check()
        if not readiness.database_ok:
            return {"result": DATABASE_UNAVAILABLE}
        if not readiness.revision_matches_head:
            return {"result": SCHEMA_NOT_AT_HEAD}
        return await _scan_history(stores)
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    engine_factory: EngineFactory = create_database_engine,
    stores_factory: StoresFactory = _postgres_stores,
    clock: Clock = _utc_now,
    config_root: str = CONFIG_ROOT,
) -> int:
    """0 表示可以切换 release；1 为任一检查未通过；2 为调用或配置错误。"""
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments:
        stderr.write(f"{_COMMAND}: usage_invalid\n")
        return 2
    try:
        settings = load_settings()
    except ConfigError:
        stderr.write(f"{_COMMAND}: configuration_error\n")
        return 2
    report: dict[str, object] | None = None
    try:
        report = asyncio.run(
            run_release_preflight(
                settings=settings,
                engine_factory=engine_factory,
                stores_factory=stores_factory,
                clock=clock,
                config_root=config_root,
            )
        )
    except DatabaseConfigurationError:
        stderr.write(f"{_COMMAND}: configuration_error\n")
        return 2
    except Exception:
        # 读路径的驱动异常可能带 SQL/参数；这里只认"数据库不可用"一个闭集结论。
        report = {"result": DATABASE_UNAVAILABLE}
    stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report["result"] == OK else 1


if __name__ == "__main__":  # pragma: no cover - 由运维一次性调用
    sys.exit(main())


__all__ = [
    "DATABASE_UNAVAILABLE",
    "HISTORICAL_EXECUTION_DATA_PRESENT",
    "OK",
    "SCHEMA_NOT_AT_HEAD",
    "TASKS_NOT_DRAINED",
    "PreflightStores",
    "main",
    "run_release_preflight",
]

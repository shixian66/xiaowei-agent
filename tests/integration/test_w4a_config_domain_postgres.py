"""W4a ``rev_0018`` 在真实 PostgreSQL 上的行为。

覆盖：旧 gemini/feishu 回执无损映射、未知 domain/零代次/主键冲突由真库裁决、OAuth
测试 context 的级联与唯一性、三个配置动作只能两阶段，以及 downgrade 在任何 DDL 之前
对 W4a 审计事实、测试 context 与 resources 回执闭集拒绝。所有降级用例都在 ``finally``
里升回 head。
"""

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.contracts import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    ConfigDomain,
    IdentitySource,
    LoadReceipt,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditStart,
    AdminAuditTerminal,
    admin_audit_target_digest,
)
from xiaowei_agent.persistence.migrations.guards import (
    MigrationPreconditionError,
    MigrationSafetyError,
)
from xiaowei_agent.persistence.postgres import (
    PostgresAdminAuditStore,
    PostgresWebSessionStore,
)
from xiaowei_agent.persistence.provider_state import (
    PostgresProviderStateStore,
    ProviderStateStoreError,
)
from xiaowei_agent.persistence.web_session import (
    ConsumeOAuthTestStateCommand,
    IssueOAuthTestStateCommand,
)

_PREVIOUS = "0017_w3_admin_query_indexes"
_HEAD = "0018_w4a_config_domains"
_NOW = dt.datetime(2026, 9, 24, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
async def restore_head(
    clean_database: AsyncEngine, alembic_runners: tuple[Any, Any]
) -> AsyncIterator[None]:
    """无论用例怎样结束，都把库升回 head，不把旧 schema 留给下一条用例。"""
    try:
        yield
    finally:
        run_upgrade, _ = alembic_runners
        async with clean_database.begin() as connection:
            await connection.run_sync(run_upgrade, "head")


async def _revision(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        value = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    return str(value)


def _config_start(operation_id: str) -> AdminAuditStart:
    return AdminAuditStart(
        operation_id=operation_id,
        tenant_id="dev-local",
        environment_id="dev",
        actor_user_id="local-admin",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
        action=AdminAuditAction.CONFIG_SAVED,
        target_kind=AdminAuditTargetKind.CONFIG,
        target_ref_digest=admin_audit_target_digest(
            target_kind=AdminAuditTargetKind.CONFIG, target_ref="config_domain:ai"
        ),
    )


# --------------------------------------------------------------------------
# 加载回执：config_domain 列与闭集
# --------------------------------------------------------------------------


async def test_receipts_round_trip_keyed_by_config_domain(
    clean_database: AsyncEngine, clock: Any
) -> None:
    store = PostgresProviderStateStore(engine=clean_database, clock=clock)
    await store.record_load(
        receipts={
            ("worker", ConfigDomain.AI): LoadReceipt(generation=2, status="loaded"),
            ("worker", ConfigDomain.RESOURCES): LoadReceipt(generation=1, status="loaded"),
            ("feishu_listener", ConfigDomain.FEISHU): LoadReceipt(
                generation=5, status="invalid"
            ),
        }
    )
    # 主键冲突是覆盖，不是第二行。
    await store.record_load(
        receipts={("worker", ConfigDomain.AI): LoadReceipt(generation=3, status="loaded")}
    )
    snapshot = await store.snapshot()
    assert snapshot.receipts == {
        ("worker", ConfigDomain.AI): LoadReceipt(generation=3, status="loaded"),
        ("worker", ConfigDomain.RESOURCES): LoadReceipt(generation=1, status="loaded"),
        ("feishu_listener", ConfigDomain.FEISHU): LoadReceipt(
            generation=5, status="invalid"
        ),
    }
    assert all(isinstance(domain, ConfigDomain) for _, domain in snapshot.receipts)


@pytest.mark.parametrize("domain", ["gemini", "provider", "AI", ""])
async def test_database_rejects_an_unknown_config_domain(
    clean_database: AsyncEngine, domain: str
) -> None:
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO service_config_state (service_name, config_domain,"
                    " loaded_generation, load_status, loaded_at)"
                    " VALUES ('worker', :domain, 1, 'loaded', :now)"
                ),
                {"domain": domain, "now": _NOW},
            )


async def test_database_rejects_a_duplicate_receipt_key_and_zero_generation(
    clean_database: AsyncEngine,
) -> None:
    insert = sa.text(
        "INSERT INTO service_config_state (service_name, config_domain,"
        " loaded_generation, load_status, loaded_at)"
        " VALUES ('worker', 'ai', :generation, 'loaded', :now)"
    )
    async with clean_database.begin() as connection:
        await connection.execute(insert, {"generation": 1, "now": _NOW})
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(insert, {"generation": 2, "now": _NOW})
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO service_config_state (service_name, config_domain,"
                    " loaded_generation, load_status, loaded_at)"
                    " VALUES ('web', 'feishu', 0, 'loaded', :now)"
                ),
                {"now": _NOW},
            )


async def test_store_rejects_an_unknown_domain_before_any_sql(
    clean_database: AsyncEngine, clock: Any
) -> None:
    store = PostgresProviderStateStore(engine=clean_database, clock=clock)
    with pytest.raises(ProviderStateStoreError):
        await store.record_load(
            receipts={("worker", "gemini"): LoadReceipt(generation=1, status="loaded")}  # type: ignore[dict-item]
        )
    assert (await store.snapshot()).receipts == {}


# --------------------------------------------------------------------------
# rev_0018 升级映射与前置条件
# --------------------------------------------------------------------------


async def test_upgrade_maps_legacy_gemini_receipts_to_ai_without_loss(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    restore_head: None,
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, _PREVIOUS)
        await connection.execute(
            sa.text(
                "INSERT INTO service_config_state (service_name, provider,"
                " loaded_generation, load_status, loaded_at) VALUES"
                " ('worker', 'gemini', 7, 'loaded', :now),"
                " ('feishu_listener', 'feishu', 4, 'invalid', :now)"
            ),
            {"now": _NOW},
        )
        await connection.run_sync(run_upgrade, "head")
    async with clean_database.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT service_name, config_domain, loaded_generation, load_status"
                    " FROM service_config_state ORDER BY service_name"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("feishu_listener", "feishu", 4, "invalid"),
        ("worker", "ai", 7, "loaded"),
    ]


async def test_upgrade_refuses_an_unknown_legacy_provider_value(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
    restore_head: None,
) -> None:
    run_upgrade, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, _PREVIOUS)
        await connection.execute(
            sa.text(
                "INSERT INTO service_config_state (service_name, provider,"
                " loaded_generation, load_status, loaded_at)"
                " VALUES ('worker', 'made_up', 1, 'loaded', :now)"
            ),
            {"now": _NOW},
        )
    with pytest.raises(MigrationPreconditionError):
        async with clean_database.begin() as connection:
            await connection.run_sync(run_upgrade, "head")
    assert await _revision(clean_database) == _PREVIOUS
    async with clean_database.begin() as connection:
        await connection.execute(sa.text("DELETE FROM service_config_state"))


async def test_clean_downgrade_restores_the_old_provider_shape(
    clean_database: AsyncEngine,
    clock: Any,
    alembic_runners: tuple[Any, Any],
    restore_head: None,
) -> None:
    store = PostgresProviderStateStore(engine=clean_database, clock=clock)
    await store.record_load(
        receipts={("worker", ConfigDomain.AI): LoadReceipt(generation=2, status="loaded")}
    )
    _, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, _PREVIOUS)
    async with clean_database.connect() as connection:
        row = (
            await connection.execute(
                sa.text("SELECT service_name, provider FROM service_config_state")
            )
        ).one()
        tables = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                    )
                )
            ).scalars()
        )
    assert tuple(row) == ("worker", "gemini")
    assert "web_oauth_test_contexts" not in tables


# --------------------------------------------------------------------------
# OAuth 测试 context
# --------------------------------------------------------------------------


async def test_test_context_cascades_and_binds_one_operation(
    clean_database: AsyncEngine, clock: Any
) -> None:
    sessions = PostgresWebSessionStore(engine=clean_database, clock=clock)
    issued = await sessions.issue_oauth_test_state(
        command=IssueOAuthTestStateCommand(
            state_digest="a" * 64,
            ttl_seconds=60,
            operation_id="w4:pg-op",
            config_generation=9,
        )
    )
    assert issued.operation_id == "w4:pg-op"
    consumed = await sessions.consume_oauth_test_state(
        command=ConsumeOAuthTestStateCommand(state_digest="a" * 64)
    )
    # P1：被测代次随 state 在真实库里往返，不在回调时按当前文件重新归属。
    assert (consumed.operation_id, consumed.config_generation) == ("w4:pg-op", 9)
    async with clean_database.begin() as connection:
        await connection.execute(sa.text("DELETE FROM web_oauth_states"))
        remaining = await connection.scalar(
            sa.text("SELECT count(*) FROM web_oauth_test_contexts")
        )
    assert remaining == 0


@pytest.mark.parametrize("operation_id", ["trace-without-prefix", "w4:" + "x" * 62])
async def test_database_rejects_a_non_w4_or_oversize_operation_id(
    clean_database: AsyncEngine, operation_id: str
) -> None:
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO web_oauth_states (state_digest, issued_at, expires_at)"
                " VALUES (:digest, :now, :later)"
            ),
            {"digest": "b" * 64, "now": _NOW, "later": _NOW + dt.timedelta(minutes=5)},
        )
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO web_oauth_test_contexts"
                    " (state_digest, operation_id, config_generation)"
                    " VALUES (:digest, :operation_id, 1)"
                ),
                {"digest": "b" * 64, "operation_id": operation_id},
            )


@pytest.mark.parametrize("config_generation", [0, -1, None])
async def test_database_rejects_a_test_context_without_a_positive_generation(
    clean_database: AsyncEngine, config_generation: int | None
) -> None:
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO web_oauth_states (state_digest, issued_at, expires_at)"
                " VALUES (:digest, :now, :later)"
            ),
            {"digest": "c" * 64, "now": _NOW, "later": _NOW + dt.timedelta(minutes=5)},
        )
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO web_oauth_test_contexts"
                    " (state_digest, operation_id, config_generation)"
                    " VALUES (:digest, 'w4:pg-generation', :generation)"
                ),
                {"digest": "c" * 64, "generation": config_generation},
            )


# --------------------------------------------------------------------------
# 两阶段配置审计
# --------------------------------------------------------------------------


async def test_config_actions_are_two_phase_in_the_real_database(
    clean_database: AsyncEngine, clock: Any
) -> None:
    audit = PostgresAdminAuditStore(engine=clean_database, clock=clock)
    started = await audit.append_started(start=_config_start("w4:pg-audit-1"))
    assert started.outcome is AdminAuditOutcome.STARTED
    terminal = await audit.append_terminal(
        terminal=AdminAuditTerminal(
            operation_id="w4:pg-audit-1",
            outcome=AdminAuditOutcome.FAILED,
            reason_code=AdminAuditReasonCode.FILE_IO_FAILED,
        )
    )
    assert terminal.action is AdminAuditAction.CONFIG_SAVED
    assert terminal.reason_code is AdminAuditReasonCode.FILE_IO_FAILED


async def test_database_still_refuses_a_started_directory_action(
    clean_database: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO admin_audit_events (event_id, operation_id, tenant_id,"
                    " environment_id, actor_user_id, actor, auth_source, action,"
                    " target_kind, target_ref_digest, outcome, created_at) VALUES"
                    " ('e1', 'op1', 't', 'dev', 'u', 'a', 'local_admin', 'role_assigned',"
                    " 'user', :digest, 'started', :now)"
                ),
                {"digest": "c" * 64, "now": _NOW},
            )


# --------------------------------------------------------------------------
# downgrade 守卫：任何 DDL 之前闭集拒绝，destructive 授权也不放行
# --------------------------------------------------------------------------


async def _audit_fact(engine: AsyncEngine, clock: Any) -> None:
    await PostgresAdminAuditStore(engine=engine, clock=clock).append_started(
        start=_config_start("w4:pg-guard")
    )


async def _test_context(engine: AsyncEngine, clock: Any) -> None:
    await PostgresWebSessionStore(engine=engine, clock=clock).issue_oauth_test_state(
        command=IssueOAuthTestStateCommand(
            state_digest="d" * 64,
            ttl_seconds=60,
            operation_id="w4:pg-guard",
            config_generation=1,
        )
    )


async def _resources_receipt(engine: AsyncEngine, clock: Any) -> None:
    await PostgresProviderStateStore(engine=engine, clock=clock).record_load(
        receipts={
            ("worker", ConfigDomain.RESOURCES): LoadReceipt(generation=1, status="loaded")
        }
    )


@pytest.mark.parametrize(
    ("seed", "category"),
    [
        (_audit_fact, "w4a_admin_audit_event"),
        (_test_context, "oauth_test_context"),
        (_resources_receipt, "resources_load_receipt"),
    ],
)
@pytest.mark.parametrize("allow_destructive", [False, True])
async def test_downgrade_refuses_w4a_facts_before_any_ddl(
    clean_database: AsyncEngine,
    clock: Any,
    alembic_runners: tuple[Any, Any],
    restore_head: None,
    seed: Any,
    category: str,
    allow_destructive: bool,
) -> None:
    await seed(clean_database, clock)
    _, run_downgrade = alembic_runners
    with pytest.raises(MigrationSafetyError) as caught:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, _PREVIOUS, allow_destructive)
    assert dict(caught.value.counts)[category] == 1
    assert await _revision(clean_database) == _HEAD
    async with clean_database.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'service_config_state'"
                    )
                )
            ).scalars()
        )
        audit_rows = await connection.scalar(sa.text("SELECT count(*) FROM admin_audit_events"))
        contexts = await connection.scalar(
            sa.text("SELECT count(*) FROM web_oauth_test_contexts")
        )
        resources_receipts = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM service_config_state"
                " WHERE config_domain = 'resources'"
            )
        )
    assert "config_domain" in columns and "provider" not in columns
    assert audit_rows == (1 if category == "w4a_admin_audit_event" else 0)
    assert contexts == (1 if category == "oauth_test_context" else 0)
    # W4b：拒绝发生在任何 DDL 之前，resources 回执原样留在库里。
    assert resources_receipts == (1 if category == "resources_load_receipt" else 0)


# --------------------------------------------------------------------------
# W4b：worker 的 resources 回执真正落库
# --------------------------------------------------------------------------


async def test_the_worker_resources_receipt_round_trips_through_postgres(
    clean_database: AsyncEngine, clock: Any, tmp_path: Any
) -> None:
    """用真实 worker 读取路径产出回执，再经 PostgreSQL 存储写入并读回。

    只在内存 fake 上绿不算证据：``config_domain`` 的 CHECK 与主键都只在真库里承重。
    """
    from xiaowei_agent.config import Settings
    from xiaowei_agent.contracts.resource_config import ResourcesConfig
    from xiaowei_agent.interfaces.integration_config_file import write_resources_config
    from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials

    target = tmp_path / "config.json"
    write_resources_config(str(target), ResourcesConfig(generation=3, resources=()))
    _, receipts = load_provider_credentials(
        settings=Settings(environment_id="dev"),
        service_name="worker",
        ai_path=str(tmp_path / "missing-ai.json"),
        resources_path=str(target),
    )
    store = PostgresProviderStateStore(engine=clean_database, clock=clock)
    await store.record_load(receipts=receipts)

    snapshot = await store.snapshot()
    receipt = snapshot.receipts[("worker", ConfigDomain.RESOURCES)]
    assert (receipt.generation, receipt.status) == (3, "loaded")
    assert set(snapshot.receipts) == {("worker", ConfigDomain.RESOURCES)}

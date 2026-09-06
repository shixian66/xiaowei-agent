"""M6b Runtime → Runner → exact Gateway → StarRocks adapter → Evidence 闭环。"""

import asyncio
import datetime as dt
from types import MappingProxyType

import pytest
from tests.fakes.clock import ManualClock

from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.capabilities.specs import SLOW_QUERY_SURFACE
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces.local_stack import (
    StarRocksLiveAssembly,
    build_in_memory_local_stack,
)
from xiaowei_agent.tools.starrocks import (
    PhysicalIdentityProbe,
    StarRocksQueryBatch,
    preflight_digest,
)

_GRANT = "GRANT SELECT ON audit_db.audit_table TO audit_reader"
_DDL = (
    "CREATE TABLE `starrocks_audit_db__`.`starrocks_audit_tbl__` "
    "(`queryId` VARCHAR(64), `timestamp` DATETIME, `queryTime` BIGINT, "
    "`scanRows` BIGINT, `returnRows` BIGINT, `scanBytes` BIGINT, "
    "`memCostBytes` BIGINT, `pendingTimeMs` BIGINT, `cpuCostNs` BIGINT, "
    "`state` VARCHAR(32), `errorCode` VARCHAR(64), `db` VARCHAR(128), "
    "`user` VARCHAR(128))"
)
_IDENTITY = "cluster-identity-1"
_IDENTITY_SQL = (
    "SELECT `cluster_identity` FROM `xiaowei_meta`.`cluster_identity_v` LIMIT 1"
)
_ROW = (
    "query-runtime-1",
    "2026-09-07 09:45:00",
    12_000,
    1_000,
    10,
    2_048,
    4_096,
    5,
    1_000_000,
    "FINISHED",
    "",
    "sales",
    "analytics_reader",
)


class _Connection:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, int | None]] = []
        self.closed = False

    def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
        self.events.append(("query", statement, max_rows))
        if statement == "SELECT VERSION()":
            return StarRocksQueryBatch(columns=("VERSION()",), rows=(("4.0.0",),))
        if statement == "SHOW GRANTS":
            return StarRocksQueryBatch(columns=("Grants",), rows=((_GRANT,),))
        if statement.startswith("SHOW CREATE TABLE"):
            return StarRocksQueryBatch(
                columns=("Table", "Create Table"),
                rows=(("starrocks_audit_tbl__", _DDL),),
            )
        if statement == _IDENTITY_SQL:
            return StarRocksQueryBatch(
                columns=("cluster_identity",), rows=((_IDENTITY,),)
            )
        if statement == "SHOW VARIABLES LIKE 'query_timeout'":
            return StarRocksQueryBatch(
                columns=("Variable_name", "Value"), rows=(("query_timeout", "20"),)
            )
        return StarRocksQueryBatch(
            columns=SLOW_QUERY_SURFACE.allowed_columns,
            rows=(_ROW,),
        )

    def execute(self, statement: str) -> None:
        self.events.append(("execute", statement, None))

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.connections: list[_Connection] = []

    def __call__(self, password: str) -> _Connection:
        self.calls.append(password)
        connection = _Connection()
        self.connections.append(connection)
        return connection


def _settings(password_file: str) -> Settings:
    return Settings(
        environment_id="test",
        actor="m6b-operator",
        starrocks_adapter_mode="test_readonly",
        starrocks_host="starrocks.test.invalid",
        starrocks_port=9030,
        starrocks_database="audit_db",
        starrocks_user="audit_reader",
        starrocks_password_file=password_file,
        starrocks_tls_mode="verify_identity",
        starrocks_ca_file="/approved/ca.pem",
        starrocks_server_name="starrocks.test.invalid",
        starrocks_resource_id="approved-test-cluster",
        starrocks_expected_grants_sha256=preflight_digest(
            (_GRANT,), order_insensitive=True
        ),
        starrocks_expected_ddl_sha256=preflight_digest(
            (_DDL,), order_insensitive=False
        ),
        starrocks_expected_identity_sha256=preflight_digest(
            (_IDENTITY,), order_insensitive=False
        ),
        starrocks_expected_metadata_source_ref="approval-ref:m6b-runtime",
        starrocks_physical_identity_ref="identity-ref:test-cluster",
        starrocks_authorized_actor="m6b-operator",
        starrocks_active_from=dt.datetime(2026, 9, 7, 9, tzinfo=dt.UTC),
        starrocks_active_until=dt.datetime(2026, 9, 7, 11, tzinfo=dt.UTC),
        starrocks_connect_timeout_seconds=5,
        starrocks_read_timeout_seconds=25,
        starrocks_write_timeout_seconds=5,
        starrocks_query_timeout_seconds=20,
    )


@pytest.mark.asyncio
async def test_real_shaped_adapter_completes_the_existing_readonly_lifecycle(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "xiaowei_agent.capabilities.target._ENVIRONMENT_DIRECTORY",
        MappingProxyType(
            {
                "dev": ("starrocks-dev-1",),
                "test": ("approved-test-cluster",),
            }
        ),
    )
    password_file = tmp_path / "database-credential"
    password_file.write_text("fixture-credential\n", encoding="utf-8")
    factory = _Factory()
    now = dt.datetime(2026, 9, 7, 10, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = _settings(str(password_file))
    stack = build_in_memory_local_stack(
        settings=settings,
        clock=clock,
        starrocks_live_assembly=StarRocksLiveAssembly(
            identity_probe=PhysicalIdentityProbe(
                statement=_IDENTITY_SQL,
                result_column="cluster_identity",
            ),
            connection_factory=factory,
            driver_version="1.2.0-test-double",
            unredacted_rows_approved=True,
        ),
    )
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="3" * 32,
        policy_revision=stack.policy_revision,
    )
    pending = await stack.runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id="request-m6b-runtime",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text="检查最近三十分钟慢查询",
                idempotency_key="idem-m6b-runtime",
                environment_id=context.environment_id,
            ),
            context=context,
            as_of=now,
        )
    )
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1

    completed = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    evidences = await stack.evidence_ledger.load(task_id=pending.task_id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.render is not None
    assert factory.calls == ["fixture-credential"]
    assert len(factory.connections) == 1
    assert factory.connections[0].closed is True
    assert len(evidences) == 1
    assert evidences[0].facts[0]["queryId"] == "query-runtime-1"
    assert evidences[0].source == "approval-ref:m6b-runtime"
    assert evidences[0].limitations[-1] == "preflight=verified"
    dumped = evidences[0].model_dump_json()
    assert "starrocks.test.invalid" not in dumped
    assert "audit_reader" not in dumped
    assert "fixture-credential" not in dumped
    assert _DDL not in dumped
    assert _GRANT not in dumped

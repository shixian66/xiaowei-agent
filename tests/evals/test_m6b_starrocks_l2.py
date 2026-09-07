"""M6b L2：注入式真实 adapter 走完整 Runtime 生命周期。"""

import asyncio
import datetime as dt
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

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

_CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "corpus" / "m6b_starrocks_l2.json").read_text(
        encoding="utf-8"
    )
)["cases"]
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
_IDENTITY_SQL = "SELECT `cluster_identity` FROM `meta`.`identity` LIMIT 1"
_ROW = (
    "query-eval-1",
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


class _ScenarioConnection:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.closed = False

    def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
        del max_rows
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
        if self.scenario == "timeout":
            raise TimeoutError("synthetic read timeout")
        if self.scenario == "permission":
            raise PermissionError("synthetic access denied")
        if "COUNT(*)" in statement:
            count = 42 if self.scenario == "empty_with_traffic" else 0
            return StarRocksQueryBatch(columns=("query_count",), rows=((count,),))
        if self.scenario == "golden":
            return StarRocksQueryBatch(
                columns=SLOW_QUERY_SURFACE.allowed_columns, rows=(_ROW,)
            )
        if self.scenario == "malformed":
            return StarRocksQueryBatch(
                columns=(*SLOW_QUERY_SURFACE.allowed_columns, "stmt"),
                rows=((*_ROW, "must-not-survive"),),
            )
        return StarRocksQueryBatch(columns=SLOW_QUERY_SURFACE.allowed_columns, rows=())

    def execute(self, statement: str) -> None:
        assert statement == "SET query_timeout = 20"

    def close(self) -> None:
        self.closed = True


class _ScenarioFactory:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.passwords: list[str] = []
        self.connections: list[_ScenarioConnection] = []

    def __call__(self, password: str) -> _ScenarioConnection:
        self.passwords.append(password)
        connection = _ScenarioConnection(self.scenario)
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
        starrocks_expected_version_sha256=preflight_digest(
            ("4.0.0",), order_insensitive=False
        ),
        starrocks_expected_grants_sha256=preflight_digest(
            (_GRANT,), order_insensitive=True
        ),
        starrocks_expected_ddl_sha256=preflight_digest(
            (_DDL,), order_insensitive=False
        ),
        starrocks_expected_identity_sha256=preflight_digest(
            (_IDENTITY,), order_insensitive=False
        ),
        starrocks_expected_metadata_source_ref="approval-ref:m6b-eval",
        starrocks_physical_identity_ref="identity-ref:test-cluster",
        starrocks_authorized_actor="m6b-operator",
        starrocks_active_from=dt.datetime(2026, 9, 7, 9, tzinfo=dt.UTC),
        starrocks_active_until=dt.datetime(2026, 9, 7, 11, tzinfo=dt.UTC),
        starrocks_connect_timeout_seconds=5,
        starrocks_read_timeout_seconds=25,
        starrocks_write_timeout_seconds=5,
        starrocks_query_timeout_seconds=20,
    )


def test_m6b_l2_corpus_is_unique_and_complete() -> None:
    assert len({case["id"] for case in _CASES}) == len(_CASES) == 6
    assert {case["scenario"] for case in _CASES} == {
        "golden",
        "empty_with_traffic",
        "empty_without_traffic",
        "timeout",
        "permission",
        "malformed",
    }


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
async def test_each_m6b_l2_case_runs_the_real_adapter_lifecycle(
    case: dict[str, Any],
    tmp_path: Path,
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
    factory = _ScenarioFactory(case["scenario"])
    now = dt.datetime(2026, 9, 7, 10, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = _settings(str(password_file))
    stack = build_in_memory_local_stack(
        settings=settings,
        clock=clock,
        starrocks_live_assembly=StarRocksLiveAssembly(
            identity_probe=PhysicalIdentityProbe(
                statement=_IDENTITY_SQL, result_column="cluster_identity"
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
        trace_id="7" * 32,
        policy_revision=stack.policy_revision,
    )
    pending = await stack.runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id=f"request-{case['scenario']}",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text="检查最近三十分钟慢查询",
                idempotency_key=f"idem-{case['scenario']}",
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
    assert completed.status is TaskStatus(case["status"])
    assert len(factory.passwords) == case["calls"]
    assert all(connection.closed for connection in factory.connections)
    joined = " ".join(evidence.model_dump_json() for evidence in evidences)
    assert "fixture-credential" not in joined
    assert "synthetic access denied" not in joined
    assert "must-not-survive" not in joined
    assert all(evidence.source == "approval-ref:m6b-eval" for evidence in evidences)

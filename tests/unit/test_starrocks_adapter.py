"""M6b StarRocks 真实只读 adapter 的无网络单元测试。"""

import datetime as dt
import stat
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST, SLOW_QUERY_SURFACE
from xiaowei_agent.contracts import AdapterStatus, RequestContext, ToolCall
from xiaowei_agent.planning.starrocks.compiler import COUNT_V1, LIST_V1, compile_sql
from xiaowei_agent.planning.starrocks.params import SlowQueryParams
from xiaowei_agent.tools import starrocks as starrocks_module
from xiaowei_agent.tools.starrocks import (
    NORMALIZER_VERSION,
    PhysicalIdentityProbe,
    PyMySQLConnectionFactory,
    StarRocksQueryBatch,
    StarRocksReadonlyAdapter,
    StarRocksReadonlyAdapterConfig,
    preflight_digest,
)

_DDL = (
    "CREATE TABLE `starrocks_audit_db__`.`starrocks_audit_tbl__` "
    "(`queryId` VARCHAR(64), `timestamp` DATETIME, `queryTime` BIGINT, "
    "`scanRows` BIGINT, `returnRows` BIGINT, `scanBytes` BIGINT, "
    "`memCostBytes` BIGINT, `pendingTimeMs` BIGINT, `cpuCostNs` BIGINT, "
    "`state` VARCHAR(32), `errorCode` VARCHAR(64), `db` VARCHAR(128), "
    "`user` VARCHAR(128))"
)
_GRANT = "GRANT SELECT ON audit_db.audit_table TO audit_reader"
_IDENTITY = "cluster-identity-1"
_EXPECTED_VERSION = "93e431573473f4fe63ecec99929bf3cb879540f81dc44f60e375c089ec6f183b"
_EXPECTED_GRANTS = "36e4233b85c65cfc23079af20150001b1a78006cba77c9ecfcc0a46536cdca1b"
_EXPECTED_DDL = "a6ea5b35f94a2093fb53917b1659560b95104117a2f519c5119a095b216298ab"
_EXPECTED_IDENTITY = "4b3402d8d058a7df949d28068cd9f2ad40961a0a075cf86be5fbfd4135abfed9"
_IDENTITY_SQL = (
    "SELECT `cluster_identity` FROM `xiaowei_meta`.`cluster_identity_v` LIMIT 1"
)
_LIST_COLUMNS = SLOW_QUERY_SURFACE.allowed_columns
_ROW = (
    "query-1",
    "2026-09-07 09:30:00",
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
_CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="m6b-operator",
    environment_id="test",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-05.2",
)
_PARAMS = SlowQueryParams(
    window_start=dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 9, 7, 10, 0, tzinfo=dt.UTC),
    min_query_time_ms=10_000,
    row_limit=20,
)


class _Connection:
    def __init__(self, batches: dict[str, StarRocksQueryBatch]) -> None:
        self._batches = batches
        self.events: list[tuple[str, str, int | None]] = []
        self.closed = False

    def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
        self.events.append(("query", statement, max_rows))
        return self._batches[statement]

    def execute(self, statement: str) -> None:
        self.events.append(("execute", statement, None))

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, connection: _Connection | None = None, error: Exception | None = None):
        self.connection = connection
        self.error = error
        self.calls: list[str] = []

    def __call__(self, password: str) -> _Connection:
        self.calls.append(password)
        if self.error is not None:
            raise self.error
        assert self.connection is not None
        return self.connection


def _write_password(path: Path, content: str = "fixture-credential") -> None:
    path.write_text(content + "\n", encoding="utf-8")


def _install_password_file_ops(
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_error: OSError | None = None,
    close_error: OSError | None = None,
) -> list[int]:
    closed: list[int] = []
    monkeypatch.setattr(starrocks_module.os, "open", lambda path, flags: 23)
    monkeypatch.setattr(
        starrocks_module.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_mode=stat.S_IFREG),
    )

    def read(descriptor: int, limit: int) -> bytes:
        if read_error is not None:
            raise read_error
        return b"fixture-credential\n"

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        if close_error is not None:
            raise close_error

    monkeypatch.setattr(starrocks_module.os, "read", read)
    monkeypatch.setattr(starrocks_module.os, "close", close)
    return closed


def _config(password_file: Path, **overrides: object) -> StarRocksReadonlyAdapterConfig:
    values: dict[str, object] = {
        "password_file": str(password_file),
        "query_timeout_seconds": 20,
        "expected_version_sha256": _EXPECTED_VERSION,
        "expected_grants_sha256": _EXPECTED_GRANTS,
        "expected_ddl_sha256": _EXPECTED_DDL,
        "expected_identity_sha256": _EXPECTED_IDENTITY,
        "audit_table": SLOW_QUERY_SURFACE.allowed_tables[0],
        "list_columns": _LIST_COLUMNS,
        "identity_probe": PhysicalIdentityProbe(
            statement=_IDENTITY_SQL,
            result_column="cluster_identity",
        ),
        "list_operation": OP_LIST,
        "count_operation": OP_COUNT,
        "list_template_id": LIST_V1,
        "count_template_id": COUNT_V1,
    }
    return StarRocksReadonlyAdapterConfig(**(values | overrides))


def _call(
    operation: str = OP_LIST,
    *,
    params: SlowQueryParams = _PARAMS,
    typed_overrides: dict[str, object] | None = None,
) -> ToolCall:
    template = LIST_V1 if operation == OP_LIST else COUNT_V1
    typed_args = {
        "sql": compile_sql(
            template_id=template,
            params=params,
            surface=SLOW_QUERY_SURFACE,
        ),
        "sql_template_id": template,
        **params.to_typed_arguments(),
    }
    return ToolCall(
        gateway="starrocks",
        operation=operation,
        step_id="s1",
        typed_args=typed_args | (typed_overrides or {}),
        timeout_seconds=30.0,
        idempotency_key="m6b-fixture",
    )


def _batches(main: StarRocksQueryBatch) -> dict[str, StarRocksQueryBatch]:
    return {
        "SELECT VERSION()": StarRocksQueryBatch(
            columns=("VERSION()",), rows=(("4.0.0",),)
        ),
        "SHOW GRANTS": StarRocksQueryBatch(columns=("Grants",), rows=((_GRANT,),)),
        "SHOW CREATE TABLE `starrocks_audit_db__`.`starrocks_audit_tbl__`": (
            StarRocksQueryBatch(
                columns=("Table", "Create Table"),
                rows=(("starrocks_audit_tbl__", _DDL),),
            )
        ),
        _IDENTITY_SQL: StarRocksQueryBatch(
            columns=("cluster_identity",), rows=((_IDENTITY,),)
        ),
        "SHOW VARIABLES LIKE 'query_timeout'": StarRocksQueryBatch(
            columns=("Variable_name", "Value"), rows=(("query_timeout", "20"),)
        ),
        _call().typed_args["sql"]: main,
        _call(OP_COUNT).typed_args["sql"]: main,
    }


def _adapter(
    password_file: Path,
    main: StarRocksQueryBatch,
    *,
    config_overrides: dict[str, object] | None = None,
) -> tuple[StarRocksReadonlyAdapter, _Factory, _Connection]:
    connection = _Connection(_batches(main))
    factory = _Factory(connection)
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file, **(config_overrides or {})),
        connection_factory=factory,
        clock=lambda: dt.datetime(2026, 9, 7, 9, 30, tzinfo=dt.UTC),
        monotonic=_TickingMonotonic(),
    )
    return adapter, factory, connection


class _TickingMonotonic:
    def __init__(self) -> None:
        self.value = 1.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.003
        return current


def test_preflight_digest_has_independent_golden_and_order_insensitive_grants() -> None:
    assert NORMALIZER_VERSION == "1"
    assert preflight_digest(("4.0.0",), order_insensitive=False) == _EXPECTED_VERSION
    assert preflight_digest((_GRANT,), order_insensitive=True) == _EXPECTED_GRANTS
    assert preflight_digest((_DDL,), order_insensitive=False) == _EXPECTED_DDL
    assert preflight_digest((_IDENTITY,), order_insensitive=False) == _EXPECTED_IDENTITY
    assert preflight_digest(("B", "A"), order_insensitive=True) == preflight_digest(
        ("A", "B"), order_insensitive=True
    )


def test_adapter_construction_does_not_read_secret_or_connect(tmp_path: Path) -> None:
    password_file = tmp_path / "not-created"
    factory = _Factory()

    StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
    )

    assert factory.calls == []


@pytest.mark.parametrize("operation", ["show_frontends", "kill_query"])
async def test_unknown_operation_is_rejected_before_secret_or_connection(
    tmp_path: Path,
    operation: str,
) -> None:
    password_file = tmp_path / "not-created"
    factory = _Factory()
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
    )

    response = await adapter.execute(
        _call().model_copy(update={"operation": operation}), context=_CONTEXT
    )

    assert response.status is AdapterStatus.ERROR
    assert factory.calls == []


@pytest.mark.parametrize(
    "pollution",
    ["host", "port", "user", "password", "tls_mode", "target", "resource_id"],
)
async def test_tool_call_cannot_override_connection_profile(
    tmp_path: Path,
    pollution: str,
) -> None:
    password_file = tmp_path / "not-created"
    factory = _Factory()
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
    )

    response = await adapter.execute(
        _call(typed_overrides={pollution: "attacker-controlled"}),
        context=_CONTEXT,
    )

    assert response.status is AdapterStatus.ERROR
    assert factory.calls == []


async def test_profile_pollution_is_rejected_even_when_connection_is_usable(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    connection = _Connection(
        _batches(StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,)))
    )
    factory = _Factory(connection)
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
    )

    response = await adapter.execute(
        _call(typed_overrides={"host": "attacker-controlled"}),
        context=_CONTEXT,
    )

    assert response.status is AdapterStatus.ERROR
    assert factory.calls == []
    assert connection.events == []


async def test_list_call_runs_fixed_preflight_then_returns_bounded_rows(tmp_path: Path) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    main = StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,))
    adapter, factory, connection = _adapter(password_file, main)

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.OK
    assert response.payload == (dict(zip(_LIST_COLUMNS, _ROW, strict=True)),)
    assert factory.calls == ["fixture-credential"]
    assert connection.events == [
        ("execute", "SET query_timeout = 20", None),
        ("query", "SHOW VARIABLES LIKE 'query_timeout'", 2),
        ("query", "SELECT VERSION()", 2),
        ("query", "SHOW GRANTS", 513),
        (
            "query",
            "SHOW CREATE TABLE `starrocks_audit_db__`.`starrocks_audit_tbl__`",
            2,
        ),
        ("query", _IDENTITY_SQL, 2),
        ("query", _call().typed_args["sql"], 21),
    ]
    assert connection.closed is True


async def test_count_call_accepts_exactly_one_row_and_rejects_other_counts(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    for rows, expected_status, expected_payload in [
        ((), AdapterStatus.ERROR, ()),
        (((7,),), AdapterStatus.OK, ({"query_count": 7},)),
    ]:
        adapter, _, connection = _adapter(
            password_file,
            StarRocksQueryBatch(columns=("query_count",), rows=rows),
        )
        response = await adapter.execute(_call(OP_COUNT), context=_CONTEXT)
        assert response.status is expected_status
        assert response.payload == expected_payload
        assert connection.closed is True

    adapter, _, connection = _adapter(
        password_file,
        StarRocksQueryBatch(columns=("query_count",), rows=((1,), (2,))),
    )
    response = await adapter.execute(_call(OP_COUNT), context=_CONTEXT)
    assert response.status is AdapterStatus.ERROR
    assert response.payload == ()
    assert connection.closed is True


async def test_version_drift_prevents_the_main_query(tmp_path: Path) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    main = StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,))
    connection = _Connection(
        _batches(main)
        | {
            "SELECT VERSION()": StarRocksQueryBatch(
                columns=("VERSION()",), rows=(("5.7.44-other-server",),)
            )
        }
    )
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=_Factory(connection),
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert not any(event[1] == _call().typed_args["sql"] for event in connection.events)
    assert connection.closed is True


@pytest.mark.parametrize(
    ("statement", "batch"),
    [
        (
            "SELECT VERSION()",
            StarRocksQueryBatch(columns=("server_version",), rows=(("4.0.0",),)),
        ),
        (
            "SHOW CREATE TABLE `starrocks_audit_db__`.`starrocks_audit_tbl__`",
            StarRocksQueryBatch(
                columns=("name", "ddl"),
                rows=(("starrocks_audit_tbl__", _DDL),),
            ),
        ),
    ],
    ids=["version_columns", "show_create_columns"],
)
async def test_preflight_rejects_unexpected_column_contracts(
    tmp_path: Path,
    statement: str,
    batch: StarRocksQueryBatch,
) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    main = StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,))
    connection = _Connection(_batches(main) | {statement: batch})
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=_Factory(connection),
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert not any(event[1] == _call().typed_args["sql"] for event in connection.events)
    assert connection.closed is True


async def test_list_rejects_row_limit_plus_one_without_partial_payload(tmp_path: Path) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    params = _PARAMS.model_copy(update={"row_limit": 1})
    main = StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW, _ROW))
    connection = _Connection(_batches(main) | {_call(params=params).typed_args["sql"]: main})
    factory = _Factory(connection)
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(params=params), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert response.payload == ()
    assert connection.closed is True


@pytest.mark.parametrize(
    "main",
    [
        StarRocksQueryBatch(columns=_LIST_COLUMNS[:-1], rows=(_ROW[:-1],)),
        StarRocksQueryBatch(columns=(*_LIST_COLUMNS, "stmt"), rows=((*_ROW, "SELECT 1"),)),
        StarRocksQueryBatch(columns=(*_LIST_COLUMNS[:-1], "queryId"), rows=(_ROW,)),
    ],
)
async def test_list_rejects_missing_extra_or_duplicate_columns(
    tmp_path: Path,
    main: StarRocksQueryBatch,
) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    adapter, _, connection = _adapter(password_file, main)

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert response.payload == ()
    assert connection.closed is True


async def test_row_codec_preserves_null_and_exact_decimal_text(tmp_path: Path) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    row = list(_ROW)
    row[1] = dt.datetime(2026, 9, 7, 9, 30, 0, 123456)
    row[3] = Decimal("1234567890.000")
    row[10] = None
    adapter, _, _ = _adapter(
        password_file,
        StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(tuple(row),)),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.OK
    assert response.payload[0]["timestamp"] == "2026-09-07 09:30:00.123456"
    assert response.payload[0]["scanRows"] == "1234567890.000"
    assert response.payload[0]["errorCode"] is None


@pytest.mark.parametrize(
    "content",
    ["", "first\nsecond", "nul\x00inside", "x" * 4097],
)
async def test_password_file_shape_fails_closed_without_connect(
    tmp_path: Path,
    content: str,
) -> None:
    password_file = tmp_path / "database-credential"
    password_file.write_text(content, encoding="utf-8")
    factory = _Factory()
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert factory.calls == []
    assert str(password_file) not in response.model_dump_json()


async def test_password_file_symlink_is_rejected_without_connect(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "database-credential"
    _write_password(target)
    link.symlink_to(target)
    factory = _Factory()
    adapter = StarRocksReadonlyAdapter(
        config=_config(link),
        connection_factory=factory,
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert factory.calls == []


def test_password_reader_closes_descriptor_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _install_password_file_ops(monkeypatch)

    assert starrocks_module._read_password_file("/run/secrets/fake") == (
        "fixture-credential"
    )
    assert closed == [23]


@pytest.mark.parametrize(
    ("read_error", "close_error"),
    [
        (OSError("private-read-detail"), None),
        (None, OSError("private-close-detail")),
        (OSError("private-read-detail"), OSError("private-close-detail")),
    ],
)
def test_password_reader_normalizes_read_and_close_failures_without_leaking_chain(
    monkeypatch: pytest.MonkeyPatch,
    read_error: OSError | None,
    close_error: OSError | None,
) -> None:
    closed = _install_password_file_ops(
        monkeypatch,
        read_error=read_error,
        close_error=close_error,
    )

    with pytest.raises(ValueError) as caught:
        starrocks_module._read_password_file("/run/secrets/fake")

    assert str(caught.value) == "credential file is unavailable or invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert closed == [23]


@pytest.mark.parametrize(
    ("override", "main_sql_runs"),
    [
        ({"expected_grants_sha256": "f" * 64}, False),
        ({"expected_ddl_sha256": "f" * 64}, False),
        ({"expected_identity_sha256": "f" * 64}, False),
        ({"query_timeout_seconds": 19}, False),
    ],
)
async def test_preflight_drift_prevents_main_query_and_closes_connection(
    tmp_path: Path,
    override: dict[str, object],
    main_sql_runs: bool,
) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    adapter, _, connection = _adapter(
        password_file,
        StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,)),
        config_overrides=override,
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert (
        any(event[1] == _call().typed_args["sql"] for event in connection.events)
        is main_sql_runs
    )
    assert connection.closed is True


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TimeoutError("read timed out"), AdapterStatus.TIMEOUT),
        (PermissionError("access denied for user with password=fixture"), AdapterStatus.ERROR),
        (ConnectionError("server disconnected"), AdapterStatus.ERROR),
    ],
)
async def test_connection_failures_are_structured_without_raw_text(
    tmp_path: Path,
    error: Exception,
    status: AdapterStatus,
) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)
    factory = _Factory(error=error)
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
        clock=lambda: dt.datetime(2026, 9, 7, 9, 30, tzinfo=dt.UTC),
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is status
    assert response.payload == ()
    assert response.error is not None
    assert "password=fixture" not in response.model_dump_json()


async def test_connection_is_closed_when_query_raises(tmp_path: Path) -> None:
    password_file = tmp_path / "database-credential"
    _write_password(password_file)

    class _ExplodingConnection(_Connection):
        def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
            raise RuntimeError("driver exploded")

    connection = _ExplodingConnection({})
    factory = _Factory(connection)
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=factory,
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert connection.closed is True


async def test_close_failure_does_not_replace_query_timeout(tmp_path: Path) -> None:
    close_marker = "private-close-marker"
    password_file = tmp_path / "database-credential"
    _write_password(password_file)

    class _TimeoutThenCloseFailure(_Connection):
        def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
            if statement == _call().typed_args["sql"]:
                raise TimeoutError("private-query-timeout")
            return super().query(statement, max_rows=max_rows)

        def close(self) -> None:
            self.closed = True
            raise RuntimeError(close_marker)

    connection = _TimeoutThenCloseFailure(
        _batches(StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,)))
    )
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=_Factory(connection),
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.TIMEOUT
    assert response.payload == ()
    assert close_marker not in response.model_dump_json()
    assert connection.closed is True


async def test_close_failure_after_success_is_safe_error(tmp_path: Path) -> None:
    close_marker = "private-close-marker"
    password_file = tmp_path / "database-credential"
    _write_password(password_file)

    class _SuccessfulThenCloseFailure(_Connection):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError(close_marker)

    connection = _SuccessfulThenCloseFailure(
        _batches(StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,)))
    )
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=_Factory(connection),
        monotonic=_TickingMonotonic(),
    )

    response = await adapter.execute(_call(), context=_CONTEXT)

    assert response.status is AdapterStatus.ERROR
    assert response.payload == ()
    assert close_marker not in response.model_dump_json()
    assert connection.closed is True


async def test_close_exception_does_not_replace_query_base_exception(
    tmp_path: Path,
) -> None:
    class StopQuery(BaseException):
        pass

    sentinel = StopQuery()
    password_file = tmp_path / "database-credential"
    _write_password(password_file)

    class _StoppedThenCloseFailure(_Connection):
        def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
            if statement == _call().typed_args["sql"]:
                raise sentinel
            return super().query(statement, max_rows=max_rows)

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("private-close-marker")

    connection = _StoppedThenCloseFailure(
        _batches(StarRocksQueryBatch(columns=_LIST_COLUMNS, rows=(_ROW,)))
    )
    adapter = StarRocksReadonlyAdapter(
        config=_config(password_file),
        connection_factory=_Factory(connection),
        monotonic=_TickingMonotonic(),
    )

    with pytest.raises(StopQuery) as caught:
        await adapter.execute(_call(), context=_CONTEXT)

    assert caught.value is sentinel
    assert connection.closed is True


def test_pymysql_factory_uses_the_closed_driver_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pymysql

    captured: dict[str, object] = {}

    class _DriverConnection:
        closed = False

        def close(self) -> None:
            self.closed = True

    driver_connection = _DriverConnection()

    def connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return driver_connection

    monkeypatch.setattr(pymysql, "connect", connect)
    factory = PyMySQLConnectionFactory(
        host="starrocks.test.invalid",
        port=9030,
        database="audit_db",
        user="audit_reader",
        tls_mode="verify_identity",
        ca_file="/approved/ca.pem",
        server_name="starrocks.test.invalid",
        connect_timeout_seconds=5,
        read_timeout_seconds=25,
        write_timeout_seconds=5,
    )

    connection = factory("fixture-credential")

    assert captured == {
        "host": "starrocks.test.invalid",
        "port": 9030,
        "user": "audit_reader",
        "password": "fixture-credential",
        "database": "audit_db",
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.Cursor,
        "connect_timeout": 5,
        "read_timeout": 25,
        "write_timeout": 5,
        "autocommit": True,
        "local_infile": False,
        "ssl_ca": "/approved/ca.pem",
        "ssl_disabled": False,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }
    connection.close()
    assert driver_connection.closed is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"tls_mode": "disabled"},
        {"server_name": "another.test.invalid"},
        {"port": 0},
        {"connect_timeout_seconds": 31},
        {"read_timeout_seconds": 30},
        {"connect_timeout_seconds": 26},
        {"write_timeout_seconds": 26},
        {"read_timeout_seconds": 1.5},
    ],
)
def test_pymysql_factory_rejects_unsafe_driver_configuration(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "host": "starrocks.test.invalid",
        "port": 9030,
        "database": "audit_db",
        "user": "audit_reader",
        "tls_mode": "verify_identity",
        "ca_file": "/approved/ca.pem",
        "server_name": "starrocks.test.invalid",
        "connect_timeout_seconds": 5,
        "read_timeout_seconds": 25,
        "write_timeout_seconds": 5,
    }
    with pytest.raises(ValueError):
        PyMySQLConnectionFactory(**(values | overrides))

"""M6b StarRocks 测试环境小结果只读 adapter。

本模块构造时不读取 credential、不导入 PyMySQL、也不打开连接。只有
``ToolGateway`` 已经精确命中 target-bound binding 后，``execute`` 才在 worker
thread 中读取有界 file-backed credential 并创建 call-local connection。
"""

import asyncio
import datetime as dt
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from types import TracebackType
from typing import Final, Protocol, Self, cast

from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError

from xiaowei_agent.contracts import (
    AdapterStatus,
    ExternalContent,
    ExternalSource,
    FrozenMap,
    JsonScalar,
    RequestContext,
    ToolCall,
)
from xiaowei_agent.redaction import safe_exception_text
from xiaowei_agent.tools.adapter import AdapterResponse

NORMALIZER_VERSION: Final[str] = "1"
_MAX_PREFLIGHT_TEXT_BYTES: Final[int] = 1_048_576
_MAX_PASSWORD_BYTES: Final[int] = 4_096
_MAX_GRANT_ROWS: Final[int] = 512
_MAX_LIST_ROWS: Final[int] = 200
_M6B_GATEWAY_TIMEOUT_SECONDS: Final[int] = 30
_SOURCE: Final[str] = "starrocks-test-readonly"
_COUNT_COLUMN: Final[str] = "query_count"
_FORBIDDEN_TOOL_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {"host", "port", "user", "password", "password_file", "tls_mode", "target", "resource_id"}
)
_PLAIN_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_NUMERIC_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "queryTime",
        "scanRows",
        "returnRows",
        "scanBytes",
        "memCostBytes",
        "pendingTimeMs",
        "cpuCostNs",
    }
)
_TEXT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"queryId", "timestamp", "state", "errorCode", "db", "user"}
)


@dataclass(frozen=True)
class StarRocksQueryBatch:
    """从 driver cursor 读取的有界列与行。"""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


class StarRocksConnection(Protocol):
    """adapter 需要的最小同步 connection 形状。"""

    def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch: ...

    def execute(self, statement: str) -> None: ...

    def close(self) -> None: ...


StarRocksConnectionFactory = Callable[[str], StarRocksConnection]


@dataclass(frozen=True)
class PhysicalIdentityProbe:
    """由代码固定的单行物理身份查询，不接受环境变量或 ToolCall 覆盖。"""

    statement: str
    result_column: str

    def __post_init__(self) -> None:
        _require_text(self.statement)
        _require_identifier(self.result_column)
        try:
            statements = parse(self.statement, read="starrocks")
        except ParseError as exc:
            raise ValueError("identity probe must be valid StarRocks SQL") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("identity probe must be one SELECT")
        query = statements[0]
        allowed_arguments = {"expressions", "from_", "limit"}
        if any(
            value is not None
            for name, value in query.args.items()
            if name not in allowed_arguments
        ):
            raise ValueError("identity probe contains an unsupported clause")
        from_clause = query.args.get("from_")
        if not isinstance(from_clause, exp.From) or not isinstance(
            from_clause.this, exp.Table
        ):
            raise ValueError("identity probe must read one table")
        table = from_clause.this
        if table.catalog or not table.db or table.alias:
            raise ValueError("identity probe table must be unaliased and database-qualified")
        _require_identifier(table.db)
        _require_identifier(table.name)
        if (
            len(query.expressions) != 1
            or not isinstance(query.expressions[0], exp.Column)
            or query.expressions[0].table
            or query.expressions[0].name != self.result_column
        ):
            raise ValueError("identity probe must project one column")
        limit = query.args.get("limit")
        if not isinstance(limit, exp.Limit) or limit.expression.name != "1":
            raise ValueError("identity probe must use LIMIT 1")


@dataclass(frozen=True)
class StarRocksReadonlyAdapterConfig:
    """不含 endpoint/账号的 adapter 执行配置。"""

    password_file: str
    query_timeout_seconds: int
    expected_version_sha256: str
    expected_grants_sha256: str
    expected_ddl_sha256: str
    expected_identity_sha256: str
    audit_table: str
    list_columns: tuple[str, ...]
    identity_probe: PhysicalIdentityProbe
    list_operation: str
    count_operation: str
    list_template_id: str
    count_template_id: str

    def __post_init__(self) -> None:
        for value in (
            self.password_file,
            self.audit_table,
            self.list_operation,
            self.count_operation,
            self.list_template_id,
            self.count_template_id,
        ):
            _require_text(value)
        for digest in (
            self.expected_version_sha256,
            self.expected_grants_sha256,
            self.expected_ddl_sha256,
            self.expected_identity_sha256,
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("preflight digest must be lowercase SHA-256")
        if not 0 < self.query_timeout_seconds <= 25:
            raise ValueError("server query timeout is outside the M6b bound")
        if not self.list_columns or len(self.list_columns) != len(set(self.list_columns)):
            raise ValueError("list columns must be non-empty and unique")
        if set(self.list_columns) != _NUMERIC_COLUMNS | _TEXT_COLUMNS:
            raise ValueError("list columns do not match the M6b slow-query surface")
        _split_table(self.audit_table)


def _require_text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError("value must be non-empty, unpadded text")
    return value


def _require_identifier(value: object) -> str:
    text = _require_text(value)
    if _PLAIN_IDENTIFIER.fullmatch(text) is None:
        raise ValueError("value must be a plain identifier")
    return text


def _split_table(value: str) -> tuple[str, str]:
    database, separator, table = value.partition(".")
    if not separator or "." in table:
        raise ValueError("audit table must be database-qualified")
    return _require_identifier(database), _require_identifier(table)


def normalize_preflight_text(value: str) -> str:
    """保留语义，只统一换行、外围空行和行尾空白。"""
    text = _require_text(value)
    if len(text.encode("utf-8")) > _MAX_PREFLIGHT_TEXT_BYTES:
        raise ValueError("preflight text exceeds the M6b bound")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


def preflight_digest(values: Sequence[str], *, order_insensitive: bool) -> str:
    """计算带 normalizer version 的 deterministic preflight 摘要。"""
    normalized = [normalize_preflight_text(value) for value in values]
    if not normalized:
        raise ValueError("preflight digest requires at least one value")
    if order_insensitive:
        normalized.sort()
    canonical = json.dumps(
        {"normalizer_version": NORMALIZER_VERSION, "values": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _show_create_statement(audit_table: str) -> str:
    database, table = _split_table(audit_table)
    statement = f"SHOW CREATE TABLE `{database}`.`{table}`"
    parsed = parse(statement, read="starrocks")
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Show):
        raise ValueError("SHOW CREATE TABLE statement is invalid")
    return statement


def _verify_ddl_surface(ddl: str, *, audit_table: str, required_columns: tuple[str, ...]) -> None:
    try:
        expression = parse_one(ddl, read="starrocks")
    except ParseError as exc:
        raise ValueError("DDL cannot be parsed as StarRocks CREATE TABLE") from exc
    if not isinstance(expression, exp.Create) or expression.args.get("kind") != "TABLE":
        raise ValueError("DDL is not CREATE TABLE")
    table = next(expression.find_all(exp.Table), None)
    if table is None:
        raise ValueError("DDL has no table")
    expected_database, expected_table = _split_table(audit_table)
    if table.db != expected_database or table.name != expected_table:
        raise ValueError("DDL table does not match the approved surface")
    columns = tuple(column.name for column in expression.find_all(exp.ColumnDef))
    if len(columns) != len(set(columns)) or not set(required_columns).issubset(columns):
        raise ValueError("DDL does not contain the approved unique columns")


def _read_password_file(path: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    payload = b""
    failed = False
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("credential reference is not a regular file")
        payload = os.read(descriptor, _MAX_PASSWORD_BYTES + 1)
    except (OSError, ValueError):
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        raise ValueError("credential file is unavailable or invalid")
    if not payload or len(payload) > _MAX_PASSWORD_BYTES:
        raise ValueError("credential file is unavailable or invalid")
    decode_failed = False
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
        value = ""
    if decode_failed:
        raise ValueError("credential file is unavailable or invalid")
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("credential file is unavailable or invalid")
    return value


def _validate_batch(batch: object) -> StarRocksQueryBatch:
    if not isinstance(batch, StarRocksQueryBatch):
        raise ValueError("driver returned a malformed query batch")
    if any(not isinstance(column, str) or not column for column in batch.columns):
        raise ValueError("driver returned malformed columns")
    if any(not isinstance(row, tuple) or len(row) != len(batch.columns) for row in batch.rows):
        raise ValueError("driver returned malformed rows")
    return batch


def _single_text(
    batch: object,
    *,
    expected_columns: tuple[str, ...] | None = None,
    value_index: int = 0,
) -> str:
    checked = _validate_batch(batch)
    if expected_columns is not None and checked.columns != expected_columns:
        raise ValueError("preflight columns do not match")
    if len(checked.rows) != 1 or value_index >= len(checked.rows[0]):
        raise ValueError("preflight must return exactly one row")
    return normalize_preflight_text(_require_text(checked.rows[0][value_index]))


def _json_value(value: object, *, column: str) -> JsonScalar:
    if value is None:
        return None
    if column == "timestamp" and isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if column in _TEXT_COLUMNS:
        if not isinstance(value, str):
            raise ValueError("driver returned an unexpected text type")
        return value
    if column in _NUMERIC_COLUMNS or column == _COUNT_COLUMN:
        if isinstance(value, bool):
            raise ValueError("driver returned an unexpected numeric type")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("driver returned a non-finite number")
            return value
        if isinstance(value, Decimal) and value.is_finite():
            return format(value, "f")
        raise ValueError("driver returned an unexpected numeric type")
    raise ValueError("driver returned an undeclared column")


def _rows_to_payload(
    batch: object,
    *,
    expected_columns: tuple[str, ...],
    max_rows: int,
) -> tuple[FrozenMap, ...]:
    checked = _validate_batch(batch)
    if checked.columns != expected_columns or len(checked.columns) != len(set(checked.columns)):
        raise ValueError("driver columns do not match the approved surface")
    if len(checked.rows) > max_rows:
        raise ValueError("driver rows exceed the approved budget")
    payload: list[dict[str, JsonScalar]] = []
    for row in checked.rows:
        payload.append(
            {
                column: _json_value(value, column=column)
                for column, value in zip(checked.columns, row, strict=True)
            }
        )
    return tuple(payload)


class StarRocksReadonlyAdapter:
    """只执行现有 slow-query 两个 operation 的 call-local adapter。"""

    def __init__(
        self,
        *,
        config: StarRocksReadonlyAdapterConfig,
        connection_factory: StarRocksConnectionFactory,
        clock: Callable[[], dt.datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))
        self._monotonic = monotonic or time.monotonic

    async def execute(self, call: ToolCall, *, context: RequestContext) -> AdapterResponse:
        started = self._monotonic()
        try:
            sql, row_limit = self._validate_call(call)
        except Exception as exc:
            return self._failure(exc, started=started)
        try:
            return await asyncio.to_thread(
                self._execute_sync,
                call,
                sql,
                row_limit,
                started,
            )
        except TimeoutError as exc:
            return self._failure(exc, started=started, timeout=True)
        except Exception as exc:
            return self._failure(exc, started=started)
        finally:
            _ = context

    def _validate_call(self, call: ToolCall) -> tuple[str, int]:
        config = self._config
        if call.operation not in {config.list_operation, config.count_operation}:
            raise ValueError("operation is not allowed by the StarRocks adapter")
        if _FORBIDDEN_TOOL_ARGUMENTS & set(call.typed_args):
            raise ValueError("ToolCall cannot override the StarRocks connection profile")
        sql = call.typed_args.get("sql")
        template_id = call.typed_args.get("sql_template_id")
        row_limit = call.typed_args.get("row_limit")
        if not isinstance(sql, str) or not sql or sql != sql.strip():
            raise ValueError("ToolCall SQL is missing or invalid")
        if not isinstance(row_limit, int) or isinstance(row_limit, bool):
            raise ValueError("ToolCall row limit is invalid")
        if not 1 <= row_limit <= _MAX_LIST_ROWS:
            raise ValueError("ToolCall row limit is outside the M6b bound")
        expected_template = (
            config.list_template_id
            if call.operation == config.list_operation
            else config.count_template_id
        )
        if template_id != expected_template:
            raise ValueError("ToolCall SQL template is not allowed")
        return sql, row_limit

    def _execute_sync(
        self,
        call: ToolCall,
        sql: str,
        row_limit: int,
        started: float,
    ) -> AdapterResponse:
        password = _read_password_file(self._config.password_file)
        connection = self._connection_factory(password)
        primary_error: BaseException | None = None
        try:
            self._preflight(connection)
            if call.operation == self._config.list_operation:
                batch = connection.query(sql, max_rows=row_limit + 1)
                payload = _rows_to_payload(
                    batch,
                    expected_columns=self._config.list_columns,
                    max_rows=row_limit,
                )
            else:
                batch = connection.query(sql, max_rows=2)
                payload = _rows_to_payload(
                    batch,
                    expected_columns=(_COUNT_COLUMN,),
                    max_rows=1,
                )
                if len(payload) != 1:
                    raise ValueError("count result must contain exactly one row")
                count = payload[0][_COUNT_COLUMN]
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError("count result is not a non-negative integer")
            return AdapterResponse(
                status=AdapterStatus.OK,
                payload=payload,
                source=_SOURCE,
                error=None,
                elapsed_ms=self._elapsed_ms(started),
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            close_failed = False
            try:
                connection.close()
            except Exception:
                close_failed = True
            if close_failed and primary_error is None:
                raise RuntimeError("StarRocks connection close failed")

    def _preflight(self, connection: StarRocksConnection) -> None:
        connection.execute(f"SET query_timeout = {self._config.query_timeout_seconds}")
        timeout = _single_text(
            connection.query("SHOW VARIABLES LIKE 'query_timeout'", max_rows=2),
            expected_columns=("Variable_name", "Value"),
            value_index=1,
        )
        if timeout != str(self._config.query_timeout_seconds):
            raise ValueError("StarRocks query_timeout readback drifted")

        version = _single_text(
            connection.query("SELECT VERSION()", max_rows=2),
            expected_columns=("VERSION()",),
        )
        if preflight_digest((version,), order_insensitive=False) != (
            self._config.expected_version_sha256
        ):
            raise ValueError("StarRocks version digest drifted")

        grants = _validate_batch(connection.query("SHOW GRANTS", max_rows=_MAX_GRANT_ROWS + 1))
        if len(grants.columns) != 1 or not 1 <= len(grants.rows) <= _MAX_GRANT_ROWS:
            raise ValueError("SHOW GRANTS returned an invalid shape")
        grant_values = tuple(_require_text(row[0]) for row in grants.rows)
        if preflight_digest(grant_values, order_insensitive=True) != (
            self._config.expected_grants_sha256
        ):
            raise ValueError("StarRocks grants digest drifted")

        ddl = _single_text(
            connection.query(_show_create_statement(self._config.audit_table), max_rows=2),
            expected_columns=("Table", "Create Table"),
            value_index=1,
        )
        if preflight_digest((ddl,), order_insensitive=False) != self._config.expected_ddl_sha256:
            raise ValueError("StarRocks DDL digest drifted")
        _verify_ddl_surface(
            ddl,
            audit_table=self._config.audit_table,
            required_columns=self._config.list_columns,
        )

        identity = _single_text(
            connection.query(self._config.identity_probe.statement, max_rows=2),
            expected_columns=(self._config.identity_probe.result_column,),
        )
        if preflight_digest((identity,), order_insensitive=False) != (
            self._config.expected_identity_sha256
        ):
            raise ValueError("StarRocks identity digest drifted")

    def _failure(
        self,
        exc: Exception,
        *,
        started: float,
        timeout: bool = False,
    ) -> AdapterResponse:
        cause = ExternalContent.capture(
            source=ExternalSource.TOOL,
            content=safe_exception_text(exc),
            captured_at=self._clock(),
        )
        return AdapterResponse(
            status=AdapterStatus.TIMEOUT if timeout else AdapterStatus.ERROR,
            payload=(),
            source=_SOURCE,
            error=cause,
            elapsed_ms=self._elapsed_ms(started),
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._monotonic() - started) * 1000))


class _DbApiCursor(Protocol):
    description: Sequence[Sequence[object]] | None

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def execute(self, statement: str) -> int: ...

    def fetchmany(self, size: int) -> Sequence[Sequence[object]]: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...

    def close(self) -> None: ...


class _PyMySQLConnection:
    def __init__(self, connection: _DbApiConnection) -> None:
        self._connection = connection

    def query(self, statement: str, *, max_rows: int) -> StarRocksQueryBatch:
        with self._connection.cursor() as cursor:
            cursor.execute(statement)
            description = cursor.description or ()
            columns = tuple(_require_text(item[0]) for item in description)
            rows = tuple(tuple(row) for row in cursor.fetchmany(max_rows))
        return StarRocksQueryBatch(columns=columns, rows=rows)

    def execute(self, statement: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(statement)

    def close(self) -> None:
        self._connection.close()


class PyMySQLConnectionFactory:
    """仅在真实 exact binding 已命中后才延迟导入并调用 PyMySQL。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        tls_mode: str,
        ca_file: str,
        server_name: str,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        write_timeout_seconds: int,
    ) -> None:
        self._host = _require_text(host)
        self._port = port
        self._database = _require_text(database)
        self._user = _require_text(user)
        if tls_mode not in {"verify_ca", "verify_identity"}:
            raise ValueError("unsupported StarRocks TLS mode")
        self._tls_mode = tls_mode
        self._ca_file = _require_text(ca_file)
        self._server_name = _require_text(server_name)
        if tls_mode == "verify_identity" and self._server_name != self._host:
            raise ValueError("PyMySQL verifies the configured host; server_name must match it")
        if not 0 < port <= 65_535:
            raise ValueError("StarRocks port is invalid")
        for timeout in (
            connect_timeout_seconds,
            read_timeout_seconds,
            write_timeout_seconds,
        ):
            if (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or not 0 < timeout < _M6B_GATEWAY_TIMEOUT_SECONDS
            ):
                raise ValueError("StarRocks driver timeout is invalid")
        if connect_timeout_seconds > read_timeout_seconds:
            raise ValueError("StarRocks connect timeout must not exceed read timeout")
        if write_timeout_seconds > read_timeout_seconds:
            raise ValueError("StarRocks write timeout must not exceed read timeout")
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._write_timeout_seconds = write_timeout_seconds

    @property
    def driver_version(self) -> str:
        from importlib.metadata import version

        return version("PyMySQL")

    def __call__(self, password: str) -> StarRocksConnection:
        import pymysql
        from pymysql.cursors import Cursor

        connection = pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=password,
            database=self._database,
            charset="utf8mb4",
            cursorclass=Cursor,
            connect_timeout=self._connect_timeout_seconds,
            read_timeout=self._read_timeout_seconds,
            write_timeout=self._write_timeout_seconds,
            autocommit=True,
            local_infile=False,
            ssl_ca=self._ca_file,
            ssl_disabled=False,
            ssl_verify_cert=True,
            ssl_verify_identity=self._tls_mode == "verify_identity",
        )
        return _PyMySQLConnection(cast(_DbApiConnection, connection))

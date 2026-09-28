"""F1 ``confirmed_readonly`` AST 路径的正反矩阵（设计 §7.3、§14.1）。

默认库 ``app``；黑名单精确列出一张表、一个视图与一个物化视图。黑名单只缩小权限面，
SHOW DATABASES / SHOW TABLES 仍可列出它们的名字。
"""

import hashlib

import pytest

from xiaowei_agent.contracts import SqlGuardRejection
from xiaowei_agent.contracts.sql_query import QualifiedRelation
from xiaowei_agent.governance.sqlguard import (
    ReadonlyPolicy,
    ReadonlyProof,
    SqlGuardError,
    verify_confirmed_readonly,
)

pytestmark = pytest.mark.security

POLICY = ReadonlyPolicy(
    default_database="app",
    blocked_relation_names=frozenset(
        {
            QualifiedRelation(database="app", name="secret"),
            QualifiedRelation(database="app", name="secret_view"),
            QualifiedRelation(database="ops", name="salary_mv"),
        }
    ),
)

R = SqlGuardRejection


def _prove(sql: str) -> ReadonlyProof:
    raw = sql.encode()
    return verify_confirmed_readonly(
        raw=raw, expected_sha256=hashlib.sha256(raw).hexdigest(), policy=POLICY
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t",
        "SELECT a, count(*) FROM t GROUP BY a HAVING count(*) > 1 ORDER BY a LIMIT 10",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT a FROM t INTERSECT SELECT a FROM u",
        "SELECT a FROM t EXCEPT SELECT a FROM u",
        "WITH x AS (SELECT a FROM t) SELECT * FROM x",
        "SELECT * FROM app.t JOIN ops.u ON t.id = u.id",
        "SELECT * FROM t WHERE a IN (SELECT b FROM ops.u)",
        "SELECT a, row_number() OVER (PARTITION BY b ORDER BY c) FROM t",
        "SELECT * FROM default_catalog.ops.t",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM sys.fe_locks",
        "SELECT * FROM _statistics_.column_statistics",
        "SELECT nosuch_builtin(a) FROM t",
        "SELECT * FROM (VALUES (1), (2)) AS v(a)",
        "WITH secret AS (SELECT 1 AS a) SELECT * FROM secret",
        "SELECT * FROM secret_archive",
        "SELECT * FROM other.secret",
        "SHOW DATABASES",
        "SHOW DATABASES FROM default_catalog",
        "SHOW TABLES",
        "SHOW FULL TABLES FROM app LIKE 'sec%'",
        "SHOW TABLE STATUS FROM ops",
        "SHOW COLUMNS FROM t",
        "SHOW FULL COLUMNS FROM ops.t",
        "SHOW INDEX FROM t",
        "SHOW CREATE TABLE t",
        "SHOW CREATE VIEW ops.v",
        "SHOW CREATE DATABASE app",
        "SHOW PROCESSLIST",
        "SHOW FULL PROCESSLIST",
        "SHOW VARIABLES LIKE 'query%'",
        "SHOW GLOBAL STATUS",
        "SHOW GRANTS",
        "SHOW ENGINES",
        "SHOW CHARSET",
        "SHOW COLLATION",
        "SHOW PLUGINS",
        "SHOW WARNINGS",
        "DESC t",
        "DESCRIBE ops.t",
        "EXPLAIN SELECT a FROM t",
        "EXPLAIN ANALYZE SELECT a FROM t JOIN ops.u ON 1 = 1",
    ],
)
def test_readonly_shapes_are_proven(sql: str) -> None:
    assert _prove(sql).path == "ast"


@pytest.mark.parametrize(
    ("sql", "rejection"),
    [
        # 黑名单：表、视图、物化视图经各种直接对象读取都拒绝，大小写与引号不影响。
        ("SELECT * FROM secret", R.BLOCKED_RELATION),
        ("SELECT * FROM app.secret", R.BLOCKED_RELATION),
        ("SELECT * FROM `APP`.`Secret`", R.BLOCKED_RELATION),
        ("SELECT * FROM default_catalog.app.secret", R.BLOCKED_RELATION),
        ("SELECT * FROM secret_view", R.BLOCKED_RELATION),
        ("SELECT * FROM ops.salary_mv", R.BLOCKED_RELATION),
        ("WITH x AS (SELECT * FROM secret) SELECT * FROM x", R.BLOCKED_RELATION),
        ("WITH secret AS (SELECT * FROM secret) SELECT 1", R.BLOCKED_RELATION),
        ("WITH a AS (SELECT * FROM secret), secret AS (SELECT 1) SELECT 1", R.BLOCKED_RELATION),
        ("SELECT * FROM t JOIN secret ON 1 = 1", R.BLOCKED_RELATION),
        ("SELECT * FROM t WHERE a IN (SELECT a FROM secret)", R.BLOCKED_RELATION),
        ("SELECT * FROM (SELECT * FROM secret) d", R.BLOCKED_RELATION),
        ("SELECT 1 UNION SELECT a FROM secret", R.BLOCKED_RELATION),
        ("EXPLAIN SELECT * FROM secret", R.BLOCKED_RELATION),
        ("EXPLAIN ANALYZE SELECT * FROM secret", R.BLOCKED_RELATION),
        ("DESC secret", R.BLOCKED_RELATION),
        ("DESC app.secret_view", R.BLOCKED_RELATION),
        ("SHOW CREATE TABLE secret", R.BLOCKED_RELATION),
        ("SHOW CREATE VIEW app.secret_view", R.BLOCKED_RELATION),
        ("SHOW COLUMNS FROM secret", R.BLOCKED_RELATION),
        ("SHOW COLUMNS FROM secret FROM app", R.BLOCKED_RELATION),
        ("SHOW FULL COLUMNS FROM app.secret", R.BLOCKED_RELATION),
        ("SHOW INDEX FROM secret", R.BLOCKED_RELATION),
        ("SHOW TABLES WHERE a IN (SELECT a FROM secret)", R.BLOCKED_RELATION),
        # 外部 Catalog：任何位置。
        ("SELECT * FROM hive.db.t", R.EXTERNAL_CATALOG),
        ("SELECT * FROM t JOIN iceberg.db.u ON 1 = 1", R.EXTERNAL_CATALOG),
        ("SELECT * FROM t WHERE a IN (SELECT a FROM hive.db.u)", R.EXTERNAL_CATALOG),
        ("SHOW DATABASES FROM hive", R.EXTERNAL_CATALOG),
        ("DESC hive.db.t", R.EXTERNAL_CATALOG),
        ("EXPLAIN SELECT * FROM hive.db.t", R.EXTERNAL_CATALOG),
        # table function、UNNEST、qualified function。
        ("SELECT * FROM TABLE(generate_series(1, 3))", R.TABLE_FUNCTION),
        ("SELECT * FROM files('path' = 's3://x')", R.TABLE_FUNCTION),
        ("SELECT * FROM unnest([1, 2])", R.TABLE_FUNCTION),
        ("SELECT * FROM t, unnest(t.a) AS u(x)", R.TABLE_FUNCTION),
        ("SELECT * FROM t LATERAL VIEW explode(a) v AS x", R.TABLE_FUNCTION),
        ("SELECT db.f(a) FROM t", R.QUALIFIED_FUNCTION),
        ("SELECT `db`.`f`(a) FROM t", R.QUALIFIED_FUNCTION),
        # 写、锁、会话改写。
        ("INSERT INTO t VALUES (1)", R.NOT_READONLY),
        ("INSERT INTO t SELECT * FROM u", R.NOT_READONLY),
        ("UPDATE t SET a = 1", R.NOT_READONLY),
        ("DELETE FROM t", R.NOT_READONLY),
        ("DROP TABLE t", R.NOT_READONLY),
        ("TRUNCATE TABLE t", R.NOT_READONLY),
        ("CREATE TABLE t2 AS SELECT * FROM t", R.NOT_READONLY),
        ("ALTER TABLE t ADD COLUMN c INT", R.NOT_READONLY),
        ("SET query_timeout = 1", R.NOT_READONLY),
        ("USE ops", R.NOT_READONLY),
        ("BEGIN", R.NOT_READONLY),
        ("KILL 1", R.NOT_READONLY),
        ("ANALYZE TABLE t", R.NOT_READONLY),
        ("GRANT SELECT ON t TO u", R.NOT_READONLY),
        ("SELECT a FROM t FOR UPDATE", R.NOT_READONLY),
        ("SELECT @a := 1", R.NOT_READONLY),
        ("EXPLAIN ANALYZE INSERT INTO t SELECT 1", R.NOT_READONLY),
        ("EXPLAIN INSERT INTO t SELECT 1", R.NOT_READONLY),
        ("SELECT a INTO OUTFILE '/tmp/x' FROM t", R.NOT_READONLY),
        # 扫描层拒绝经同一入口给出闭集原因。
        ("SELECT 1; DROP TABLE t", R.MULTIPLE_STATEMENTS),
        ("SELECT /*+ SET_VAR(query_timeout=1) */ 1", R.HINT_PRESENT),
        ("SELECT a，b FROM t", R.AMBIGUOUS_CHARACTER),
        ("SELECT '​'", R.AMBIGUOUS_CHARACTER),
        # 非允许根节点与 parser 不认识的语句。
        ("HELP 'show'", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("SHOW BINARY LOGS", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("SHOW CREATE PROCEDURE p", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("SHOW TABLES FROM hive.db", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("DESC FORMATTED t", R.READONLY_STATEMENT_NOT_SUPPORTED),
        # DESC 只描述对象，EXPLAIN 只包装查询；parser 允许的 MySQL 互换写法不放行。
        ("EXPLAIN t", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("EXPLAIN ops.t", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("DESC SELECT 1", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("DESCRIBE SELECT a FROM t", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("SHOW USERS", R.READONLY_STATEMENT_NOT_SUPPORTED),
        ("SET CATALOG hive", R.READONLY_STATEMENT_NOT_SUPPORTED),
    ],
)
def test_unsafe_shapes_are_rejected(sql: str, rejection: SqlGuardRejection) -> None:
    with pytest.raises(SqlGuardError) as caught:
        _prove(sql)
    assert caught.value.rejection is rejection


def test_hash_mismatch_is_rejected() -> None:
    with pytest.raises(SqlGuardError) as caught:
        verify_confirmed_readonly(
            raw=b"SELECT 1",
            expected_sha256=hashlib.sha256(b"SELECT 2").hexdigest(),
            policy=POLICY,
        )
    assert caught.value.rejection is R.HASH_MISMATCH


def test_blocked_names_are_still_listable() -> None:
    # 黑名单不提供元数据保密：列举库与表名照常放行。
    assert _prove("SHOW TABLES FROM app").relations == ()
    assert _prove("SHOW TABLES LIKE 'secret'").relations == ()


def test_deep_nesting_is_a_closed_rejection_not_a_crash() -> None:
    # 解析器递归溢出必须落到闭集拒绝码上，而不是让 RecursionError 冒出准入边界。
    sql = "SELECT " + "(" * 20_000 + "1" + ")" * 20_000
    assert len(sql.encode()) < 65_536
    with pytest.raises(SqlGuardError) as caught:
        _prove(sql)
    assert caught.value.rejection is R.UNPARSABLE

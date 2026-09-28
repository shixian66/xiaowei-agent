"""F1 代码内只读语句清单的正反矩阵（计划 §1 A1–A12、B1–B9，设计 §7.3 清单路径）。

每个清单项至少一个正常对照，并覆盖大小写/空白/反引号边界；带对象的项覆盖黑名单、外部
Catalog 与歧义目标；每项覆盖缺失或重复 clause。清单外一律 ``READONLY_STATEMENT_NOT_SUPPORTED``。
"""

import hashlib

import pytest

from xiaowei_agent.contracts import SqlGuardRejection
from xiaowei_agent.contracts.sql_query import QualifiedRelation
from xiaowei_agent.governance.readonly_statements import READONLY_STATEMENTS
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
            QualifiedRelation(database="ops", name="salary_mv"),
        }
    ),
)

R = SqlGuardRejection
NOT_SUPPORTED = R.READONLY_STATEMENT_NOT_SUPPORTED


def _prove(sql: str) -> ReadonlyProof:
    raw = sql.encode()
    return verify_confirmed_readonly(
        raw=raw, expected_sha256=hashlib.sha256(raw).hexdigest(), policy=POLICY
    )


def _rejection(sql: str) -> SqlGuardRejection:
    with pytest.raises(SqlGuardError) as caught:
        _prove(sql)
    return caught.value.rejection


# (family, sql, 期望的直接对象)
ACCEPTED: list[tuple[str, str, set[tuple[str, str]]]] = [
    # A1
    ("show.create_materialized_view", "SHOW CREATE MATERIALIZED VIEW mv", {("app", "mv")}),
    ("show.create_materialized_view", "show create materialized view ops.mv", {("ops", "mv")}),
    (
        "show.create_materialized_view",
        "SHOW CREATE MATERIALIZED VIEW default_catalog.`ops`.`m v`",
        {("ops", "m v")},
    ),
    # A2
    ("show.materialized_views", "SHOW MATERIALIZED VIEWS", set()),
    ("show.materialized_views", "SHOW MATERIALIZED VIEWS FROM ops", set()),
    ("show.materialized_views", "SHOW MATERIALIZED VIEWS IN ops LIKE 'sal%'", set()),
    ("show.materialized_views", "SHOW MATERIALIZED VIEWS WHERE NAME LIKE 'sal%'", set()),
    (
        "show.materialized_views",
        "SHOW MATERIALIZED VIEWS FROM ops WHERE NAME = 'mv'",
        {("ops", "mv")},
    ),
    ("show.materialized_views", "SHOW MATERIALIZED VIEWS WHERE name = 'mv'", {("app", "mv")}),
    # A3
    ("show.partitions", "SHOW PARTITIONS FROM t", {("app", "t")}),
    ("show.partitions", "SHOW TEMPORARY PARTITIONS FROM ops.t", {("ops", "t")}),
    # A4
    ("show.tablet", "SHOW TABLET FROM ops.t", {("ops", "t")}),
    ("show.tablet_id", "SHOW TABLET 10086", set()),
    # A5
    ("show.data", "SHOW DATA", set()),
    ("show.data", "SHOW DATA FROM ops", set()),
    ("show.data", "SHOW DATA FROM ops.t", {("ops", "t")}),
    # A6
    ("show.load", "SHOW LOAD", set()),
    ("show.load", "SHOW LOAD FROM ops", set()),
    ("show.routine_load", "SHOW ROUTINE LOAD FROM ops", set()),
    ("show.functions", "SHOW FUNCTIONS", set()),
    ("show.functions", "SHOW FULL FUNCTIONS IN ops LIKE 'f%'", set()),
    # A7
    ("show.catalogs", "SHOW CATALOGS", set()),
    # A8
    ("show.frontends", "SHOW FRONTENDS", set()),
    ("show.backends", "show backends", set()),
    ("show.resource_groups", "SHOW RESOURCE GROUPS", set()),
    ("show.proc", "SHOW PROC '/backends'", set()),
    ("show.profilelist", "SHOW PROFILELIST", set()),
    ("show.profilelist", "SHOW PROFILELIST LIMIT 20", set()),
    # A9
    ("show.alter_table", "SHOW ALTER TABLE COLUMN", set()),
    ("show.alter_table", "SHOW ALTER TABLE ROLLUP FROM ops", set()),
    (
        "show.alter_table",
        "SHOW ALTER TABLE MATERIALIZED VIEW FROM ops WHERE TableName = 't'",
        {("ops", "t")},
    ),
    # A10
    ("admin.show_replica_status", "ADMIN SHOW REPLICA STATUS FROM ops.t", {("ops", "t")}),
    ("admin.show_replica_distribution", "ADMIN SHOW REPLICA DISTRIBUTION FROM t", {("app", "t")}),
    # A11
    ("analyze.profile", "ANALYZE PROFILE FROM 'b2f1c3d4-0000-0000-0000-000000000000'", set()),
    # A12
    ("explain.modifier", "EXPLAIN LOGICAL SELECT a FROM t", {("app", "t")}),
    (
        "explain.modifier",
        "EXPLAIN VERBOSE SELECT * FROM ops.u JOIN t ON 1 = 1",
        {("ops", "u"), ("app", "t")},
    ),
    ("explain.modifier", "explain costs WITH x AS (SELECT 1) SELECT * FROM x", set()),
    # B1
    ("show.running_queries", "SHOW RUNNING QUERIES", set()),
    # B2
    ("show.analyze_status", "SHOW ANALYZE STATUS", set()),
    (
        "show.analyze_status",
        "SHOW ANALYZE STATUS WHERE `Database` = 'ops' AND Status = 'FAILED'",
        set(),
    ),
    ("show.stats_meta", "SHOW STATS META WHERE `Table` = 'secret'", set()),
    # B3
    ("show.dynamic_partition_tables", "SHOW DYNAMIC PARTITION TABLES FROM ops", set()),
    # B4
    ("show.delete", "SHOW DELETE FROM ops", set()),
    ("show.export", "SHOW EXPORT", set()),
    # B5
    ("show.transaction", "SHOW TRANSACTION WHERE id = 42", set()),
    ("show.transaction", "SHOW TRANSACTION FROM ops WHERE ID = 42", set()),
    # B6
    ("show.compute_nodes", "SHOW COMPUTE NODES", set()),
    # B7
    ("show.roles", "SHOW ROLES", set()),
    # B8：作业名不是 relation，不查黑名单。
    ("show.create_routine_load", "SHOW CREATE ROUTINE LOAD ops.job1", set()),
    ("show.create_routine_load", "SHOW CREATE ROUTINE LOAD secret", set()),
    # B9
    ("describe.all", "DESC ops.t ALL", {("ops", "t")}),
    ("describe.all", "describe  `t`  all ;", {("app", "t")}),
]


@pytest.mark.parametrize(("family", "sql", "relations"), ACCEPTED)
def test_listed_statements_are_proven(
    family: str, sql: str, relations: set[tuple[str, str]]
) -> None:
    proof = _prove(sql)
    assert proof.path == "statement_list"
    assert proof.statement_family == family
    assert {(r.database, r.name) for r in proof.relations} == relations


def test_every_listed_family_has_a_positive_case() -> None:
    assert {family for family, _sql, _relations in ACCEPTED} == {
        statement.family for statement in READONLY_STATEMENTS
    }


@pytest.mark.parametrize(
    ("sql", "rejection"),
    [
        # 黑名单：带对象的清单项。
        ("SHOW CREATE MATERIALIZED VIEW ops.salary_mv", R.BLOCKED_RELATION),
        ("SHOW CREATE MATERIALIZED VIEW `OPS`.`Salary_MV`", R.BLOCKED_RELATION),
        ("SHOW MATERIALIZED VIEWS FROM ops WHERE NAME = 'salary_mv'", R.BLOCKED_RELATION),
        ("SHOW PARTITIONS FROM secret", R.BLOCKED_RELATION),
        ("SHOW TABLET FROM app.secret", R.BLOCKED_RELATION),
        ("SHOW DATA FROM app.secret", R.BLOCKED_RELATION),
        ("SHOW ALTER TABLE COLUMN WHERE TableName = 'secret'", R.BLOCKED_RELATION),
        ("ADMIN SHOW REPLICA STATUS FROM secret", R.BLOCKED_RELATION),
        ("ADMIN SHOW REPLICA DISTRIBUTION FROM app.secret", R.BLOCKED_RELATION),
        ("DESC secret ALL", R.BLOCKED_RELATION),
        ("EXPLAIN LOGICAL SELECT * FROM secret", R.BLOCKED_RELATION),
        (
            "EXPLAIN VERBOSE SELECT * FROM t WHERE a IN (SELECT a FROM app.secret)",
            R.BLOCKED_RELATION,
        ),
        # 外部 Catalog。
        ("SHOW CREATE MATERIALIZED VIEW hive.db.mv", R.EXTERNAL_CATALOG),
        ("SHOW PARTITIONS FROM hive.db.t", R.EXTERNAL_CATALOG),
        ("ADMIN SHOW REPLICA STATUS FROM iceberg.db.t", R.EXTERNAL_CATALOG),
        ("DESC hive.db.t ALL", R.EXTERNAL_CATALOG),
        ("EXPLAIN COSTS SELECT * FROM hive.db.t", R.EXTERNAL_CATALOG),
        # EXPLAIN 修饰词的内层：写、文件、table function。
        ("EXPLAIN LOGICAL INSERT INTO t SELECT 1", R.NOT_READONLY),
        ("EXPLAIN VERBOSE DELETE FROM t", R.NOT_READONLY),
        ("EXPLAIN LOGICAL SELECT a INTO OUTFILE '/tmp/x' FROM t", R.NOT_READONLY),
        ("EXPLAIN COSTS SELECT * FROM files('path' = 's3://x')", R.TABLE_FUNCTION),
        ("EXPLAIN LOGICAL SHOW TABLES", NOT_SUPPORTED),
        ("EXPLAIN LOGICAL", NOT_SUPPORTED),
        # 多语句与 hint 在清单之前就被拒绝。
        ("SHOW CATALOGS; SHOW ROLES", R.MULTIPLE_STATEMENTS),
        ("SHOW PARTITIONS FROM t; DROP TABLE t", R.MULTIPLE_STATEMENTS),
        ("SHOW /*+ SET_VAR(a=1) */ CATALOGS", R.HINT_PRESENT),
        # 缺失 clause。
        ("SHOW CREATE MATERIALIZED VIEW", NOT_SUPPORTED),
        ("SHOW PARTITIONS", NOT_SUPPORTED),
        ("SHOW PARTITIONS FROM", NOT_SUPPORTED),
        ("SHOW TABLET", NOT_SUPPORTED),
        ("SHOW PROC", NOT_SUPPORTED),
        ("ANALYZE PROFILE FROM", NOT_SUPPORTED),
        ("ADMIN SHOW REPLICA STATUS", NOT_SUPPORTED),
        ("SHOW TRANSACTION", NOT_SUPPORTED),
        ("SHOW TRANSACTION FROM ops", NOT_SUPPORTED),
        ("DESC ops.t ALL ALL", NOT_SUPPORTED),
        ("SHOW ALTER TABLE", NOT_SUPPORTED),
        # 重复 clause 与多余 token。
        ("SHOW CATALOGS CATALOGS", NOT_SUPPORTED),
        ("SHOW MATERIALIZED VIEWS FROM ops FROM app", NOT_SUPPORTED),
        ("SHOW MATERIALIZED VIEWS LIKE 'a' LIKE 'b'", NOT_SUPPORTED),
        ("SHOW DATA FROM ops FROM app", NOT_SUPPORTED),
        ("SHOW PROFILELIST LIMIT 1 LIMIT 2", NOT_SUPPORTED),
        ("SHOW FRONTENDS WHERE 1 = 1", NOT_SUPPORTED),
        ("SHOW PARTITIONS FROM t WHERE PartitionName = 'p1'", NOT_SUPPORTED),
        # 歧义或无法唯一提取的目标。
        ("SHOW PARTITIONS FROM a.b.c.d", NOT_SUPPORTED),
        ("SHOW PARTITIONS FROM ops.", NOT_SUPPORTED),
        ("SHOW PARTITIONS FROM ``", NOT_SUPPORTED),
        ("SHOW DATA FROM default_catalog.ops.t", NOT_SUPPORTED),
        ("SHOW MATERIALIZED VIEWS WHERE NAME = 'a\\'b'", NOT_SUPPORTED),
        ("SHOW MATERIALIZED VIEWS WHERE NAME = ''", NOT_SUPPORTED),
        ("SHOW MATERIALIZED VIEWS WHERE NAME = mv", NOT_SUPPORTED),
        # 登记列与字面量之外的 WHERE。
        ("SHOW ANALYZE STATUS WHERE Properties = 'x'", NOT_SUPPORTED),
        ("SHOW ANALYZE STATUS WHERE Status = 'a' OR Status = 'b'", NOT_SUPPORTED),
        ("SHOW STATS META WHERE `Table` = (SELECT a FROM secret)", NOT_SUPPORTED),
        ("SHOW STATS META WHERE `Table` LIKE 'x'", NOT_SUPPORTED),
        ("SHOW TRANSACTION WHERE id = '42'", NOT_SUPPORTED),
        ("SHOW TRANSACTION WHERE label = 'x'", NOT_SUPPORTED),
        ("SHOW TABLET abc", NOT_SUPPORTED),
        # 清单外：没有“以 SHOW 开头即放行”。
        ("SHOW USERS", NOT_SUPPORTED),
        ("SHOW RESOURCES", NOT_SUPPORTED),
        ("SHOW STORAGE VOLUMES", NOT_SUPPORTED),
        ("SHOW WAREHOUSES", NOT_SUPPORTED),
        ("SHOW PIPES", NOT_SUPPORTED),
        ("SHOW STREAM LOAD", NOT_SUPPORTED),
        ("SHOW BROKER", NOT_SUPPORTED),
        ("SHOW REPOSITORIES", NOT_SUPPORTED),
        ("SHOW BACKUP", NOT_SUPPORTED),
        ("SHOW RESTORE", NOT_SUPPORTED),
        ("SHOW PROPERTY", NOT_SUPPORTED),
        ("SHOW HISTOGRAM META", NOT_SUPPORTED),
        ("SHOW VIEWS", NOT_SUPPORTED),
        ("SHOW", NOT_SUPPORTED),
        ("SHOW anything_else", NOT_SUPPORTED),
        ("ADMIN SET FRONTEND CONFIG ('a' = 'b')", NOT_SUPPORTED),
        ("ADMIN SHOW FRONTEND CONFIG", NOT_SUPPORTED),
        ("CANCEL LOAD FROM ops", NOT_SUPPORTED),
        ("PAUSE ROUTINE LOAD FOR job1", NOT_SUPPORTED),
        ("SUBMIT TASK AS INSERT INTO t SELECT 1", NOT_SUPPORTED),
        ("REFRESH MATERIALIZED VIEW mv", R.NOT_READONLY),
    ],
)
def test_unlisted_or_unsafe_statements_are_rejected(
    sql: str, rejection: SqlGuardRejection
) -> None:
    assert _rejection(sql) is rejection


def test_a_long_registered_where_chain_is_matched_without_recursion() -> None:
    # 64 KiB 内可以拼出几千个 AND；匹配必须给出闭集结果，不能抛 RecursionError。
    predicate = " AND `Table` = 'a'"
    sql = "SHOW STATS META WHERE `Table` = 'a'" + predicate * 3000
    assert len(sql.encode()) < 65_536
    assert _prove(sql).statement_family == "show.stats_meta"
    assert _rejection(sql + " AND Properties = 'x'") is NOT_SUPPORTED

"""``confirmed_readonly`` 的证明形状与关系提取（设计 §7.3）。"""

import dataclasses
import hashlib
import logging

import pytest

from xiaowei_agent.contracts import SqlGuardRejection
from xiaowei_agent.contracts.sql_query import QualifiedRelation
from xiaowei_agent.governance.sqlguard import (
    ReadonlyPolicy,
    ReadonlyProof,
    SqlGuardError,
    verify_confirmed_readonly,
)

POLICY = ReadonlyPolicy(
    default_database="app",
    blocked_relation_names=frozenset({QualifiedRelation(database="app", name="secret")}),
)


def _prove(sql: str, policy: ReadonlyPolicy = POLICY) -> ReadonlyProof:
    raw = sql.encode()
    return verify_confirmed_readonly(
        raw=raw, expected_sha256=hashlib.sha256(raw).hexdigest(), policy=policy
    )


def _relations(sql: str) -> set[tuple[str, str]]:
    return {(r.database, r.name) for r in _prove(sql).relations}


def test_proof_carries_no_sql_text() -> None:
    proof = _prove("SELECT a FROM t")
    assert {f.name for f in dataclasses.fields(proof)} == {
        "path",
        "statement_family",
        "relations",
    }
    assert proof.path == "ast"
    assert proof.statement_family == "query"


def test_relation_names_resolve_against_the_default_database() -> None:
    assert _relations("SELECT a FROM t") == {("app", "t")}
    assert _relations("SELECT a FROM ops.t") == {("ops", "t")}
    assert _relations("SELECT a FROM default_catalog.ops.t") == {("ops", "t")}
    assert _relations("SELECT a FROM DEFAULT_CATALOG.Ops.T") == {("ops", "t")}
    assert _relations("SELECT * FROM `ops`.`t` JOIN app.u ON 1 = 1") == {
        ("ops", "t"),
        ("app", "u"),
    }


def test_cte_aliases_and_derived_tables_are_not_base_relations() -> None:
    assert _relations("WITH x AS (SELECT a FROM t) SELECT * FROM x") == {("app", "t")}
    assert _relations("SELECT * FROM (SELECT a FROM t) AS d") == {("app", "t")}
    assert _relations(
        "WITH a AS (SELECT 1), b AS (SELECT * FROM a) SELECT * FROM b"
    ) == set()


def test_a_cte_is_only_visible_after_its_definition() -> None:
    # 非递归 CTE 的正文里，自身名与后面定义的 CTE 名都指向真实表。
    assert _relations("WITH x AS (SELECT * FROM x) SELECT * FROM x") == {("app", "x")}
    assert _relations(
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1) SELECT * FROM a"
    ) == {("app", "b")}
    # CTE 名按原样区分大小写；对不上的名字当真实表检查，只会多拒不会漏拒。
    assert _relations("WITH X AS (SELECT 1) SELECT * FROM x") == {("app", "x")}
    # CTE 只在定义它的查询内可见。
    assert _relations(
        "SELECT * FROM (WITH x AS (SELECT 1) SELECT * FROM x) d JOIN x ON 1 = 1"
    ) == {("app", "x")}


def test_show_and_describe_families() -> None:
    assert _prove("SHOW DATABASES").statement_family == "show.databases"
    assert _prove("show tables from ops").statement_family == "show.tables"
    assert _relations("SHOW COLUMNS FROM ops.t") == {("ops", "t")}
    assert _relations("SHOW CREATE TABLE t") == {("app", "t")}
    assert _relations("DESC ops.t") == {("ops", "t")}
    assert _prove("DESC ops.t").statement_family == "describe"
    assert _prove("EXPLAIN SELECT a FROM t").statement_family == "explain"
    assert _prove("EXPLAIN ANALYZE SELECT a FROM t").statement_family == "explain.analyze"
    assert _relations("EXPLAIN SELECT a FROM ops.t") == {("ops", "t")}


def test_trailing_semicolon_and_comments_are_accepted() -> None:
    assert _prove("-- lead\nSELECT 1; -- tail").statement_family == "query"


def test_rejection_message_never_echoes_the_sql() -> None:
    with pytest.raises(SqlGuardError) as caught:
        _prove("SELECT canary_secret FROM app.secret")
    assert caught.value.rejection is SqlGuardRejection.BLOCKED_RELATION
    assert "canary" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_parser_fallback_never_logs_the_sql(caplog: pytest.LogCaptureFixture) -> None:
    # sqlglot 回退为 Command 时会把原文写进 warning；SQL 原文不得进入日志。
    with caplog.at_level(logging.DEBUG), pytest.raises(SqlGuardError):
        _prove("SHOW canary_secret_statement")
    assert "canary" not in caplog.text


def test_hash_is_checked_before_anything_else() -> None:
    raw = b"SELECT 1; DROP TABLE t"
    with pytest.raises(SqlGuardError) as caught:
        verify_confirmed_readonly(raw=raw, expected_sha256="0" * 64, policy=POLICY)
    assert caught.value.rejection is SqlGuardRejection.HASH_MISMATCH

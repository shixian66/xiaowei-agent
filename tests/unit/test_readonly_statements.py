"""只读语句清单匹配器的结构性质。"""

import pytest

from xiaowei_agent.governance.readonly_statements import (
    READONLY_STATEMENTS,
    Kw,
    Many,
    Name,
    Opt,
    ReadonlyStatement,
    RelationRef,
    Rest,
    TargetRule,
    _check_target_rule,
    match_statement,
)
from xiaowei_agent.governance.sql_tokens import scan_sql


def test_relation_parts_keep_their_positions() -> None:
    match = match_statement(scan_sql(b"SHOW PARTITIONS FROM default_catalog.ops.t"))
    assert match is not None
    assert match.relations == (RelationRef(catalog="default_catalog", database="ops", name="t"),)
    match = match_statement(scan_sql(b"SHOW PARTITIONS FROM t"))
    assert match is not None
    assert match.relations == (RelationRef(catalog="", database="", name="t"),)


def test_explain_modifier_captures_the_exact_inner_bytes() -> None:
    raw = "EXPLAIN  VERBOSE /* 说明 */ SELECT '中' FROM t -- tail\n;".encode()
    match = match_statement(scan_sql(raw))
    assert match is not None and match.inner_query is not None
    start, end = match.inner_query
    assert raw[start:end] == "SELECT '中' FROM t".encode()


def test_comments_between_keywords_do_not_change_the_match() -> None:
    match = match_statement(scan_sql(b"SHOW /* a */ RUNNING -- b\n QUERIES"))
    assert match is not None
    assert match.family == "show.running_queries"


def test_a_statement_with_two_distinct_matches_is_ambiguous() -> None:
    # 清单本身无歧义；这里用本地文法证明“多种匹配即不匹配”的规则确实生效。
    import xiaowei_agent.governance.readonly_statements as module

    ambiguous = (
        ReadonlyStatement(
            "test.a",
            (Kw("SHOW", "X"), Name("relation", 1, 2), Opt(Kw("Y"))),
            TargetRule.RELATION,
        ),
        ReadonlyStatement(
            "test.b",
            (Kw("SHOW", "X"), Name("relation", 1, 1), Opt(Kw("Y"))),
            TargetRule.RELATION,
        ),
    )
    original = module.READONLY_STATEMENTS
    module.READONLY_STATEMENTS = ambiguous
    try:
        assert match_statement(scan_sql(b"SHOW X a")) is None
        assert match_statement(scan_sql(b"SHOW X a.b")) is not None
    finally:
        module.READONLY_STATEMENTS = original


@pytest.mark.parametrize(
    "statement",
    [
        ReadonlyStatement("bad.none", (Kw("SHOW"), Name("relation")), TargetRule.NONE),
        ReadonlyStatement("bad.rel", (Kw("SHOW"), Opt(Name("relation"))), TargetRule.RELATION),
        ReadonlyStatement("bad.opt", (Kw("SHOW"),), TargetRule.OPTIONAL_RELATION),
        ReadonlyStatement("bad.inner", (Kw("EXPLAIN"),), TargetRule.INNER_QUERY),
        ReadonlyStatement(
            "bad.inner_rel",
            (Kw("EXPLAIN"), Name("relation"), Rest("inner_query")),
            TargetRule.INNER_QUERY,
        ),
    ],
)
def test_target_rule_must_match_the_pattern_captures(statement: ReadonlyStatement) -> None:
    with pytest.raises(RuntimeError):
        _check_target_rule(statement)


def test_listed_families_are_unique_and_rules_hold() -> None:
    families = [statement.family for statement in READONLY_STATEMENTS]
    assert len(families) == len(set(families))
    for statement in READONLY_STATEMENTS:
        _check_target_rule(statement)


def test_repetition_cannot_capture() -> None:
    with pytest.raises(RuntimeError):
        Many(Kw("AND"), Name("relation"))

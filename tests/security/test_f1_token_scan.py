"""F1 原文 token 扫描的恶意矩阵（设计 §7.2）。

扫描在 AST 之前运行，负责 AST 看不见或解析器可能看错的东西：语句边界、hint 注释、
控制字符与全角/智能引号、字符串外的 INTO OUTFILE。
"""

import pytest

from xiaowei_agent.governance.sql_tokens import TokenScanError, TokenScanReason, scan_sql

pytestmark = pytest.mark.security


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT 1 ;  \n",
        "SELECT 1; -- trailing comment",
        "SELECT 1; /* trailing */ # and more",
        "SELECT ';' FROM t",
        'SELECT ";" FROM t',
        "SELECT `a;b` FROM t",
        "SELECT 1 -- ; DROP TABLE t",
        "SELECT 1 # ; DROP TABLE t",
        "SELECT 1 /* ; DROP TABLE t */",
        "SELECT '/*+ not a hint */' FROM t",
        "SELECT '/*! not executable */' FROM t",
        "SELECT `/*+x*/` FROM t",
        "SELECT 1 /* + spaced is a plain comment */",
        "SELECT 'into outfile' FROM t",
        "SELECT 1 -- INTO OUTFILE '/tmp/x'",
        "SELECT `into` FROM `outfile`",
        "SELECT '中文，全角“引号”' FROM t -- 中文注释：好",
        "SELECT a\tFROM t\r\nWHERE b = 1",
    ],
)
def test_accepted_shapes(sql: str) -> None:
    scan_sql(sql.encode())


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"", TokenScanReason.EMPTY),
        (b"   \n", TokenScanReason.EMPTY),
        (b"-- only a comment", TokenScanReason.EMPTY),
        (b";", TokenScanReason.EMPTY),
        (b"SELECT 1; SELECT 2", TokenScanReason.MULTI_STATEMENT),
        (b"SELECT 1;;", TokenScanReason.MULTI_STATEMENT),
        (b"SELECT 1; DROP TABLE t", TokenScanReason.MULTI_STATEMENT),
        (b"SELECT 1 /* c */ ; /* c */ DELETE FROM t", TokenScanReason.MULTI_STATEMENT),
        (b"SELECT 'a'; SELECT 'b'", TokenScanReason.MULTI_STATEMENT),
        (b"SELECT /*+ SET_VAR(query_timeout=1) */ 1", TokenScanReason.HINT_COMMENT),
        (b"SELECT /*! 50000 1 */", TokenScanReason.HINT_COMMENT),
        (b"/*+x*/ SELECT 1", TokenScanReason.HINT_COMMENT),
        (b"SELECT 1 /*+ trailing */", TokenScanReason.HINT_COMMENT),
        (b"SELECT a INTO OUTFILE '/tmp/x' FROM t", TokenScanReason.INTO_OUTFILE),
        (b"SELECT a into  outfile 'x' FROM t", TokenScanReason.INTO_OUTFILE),
        (b"SELECT a INTO /* c */ OUTFILE 'x' FROM t", TokenScanReason.INTO_OUTFILE),
        (b"SELECT a INTO DUMPFILE 'x' FROM t", TokenScanReason.INTO_OUTFILE),
        (b"SELECT \xff", TokenScanReason.INVALID_UTF8),
        (b"SELECT '\xc3'", TokenScanReason.INVALID_UTF8),
        ("﻿SELECT 1".encode(), TokenScanReason.BOM),
        ("SELECT '﻿'".encode(), TokenScanReason.BOM),
        (b"SELECT\x001", TokenScanReason.CONTROL_CHARACTER),
        (b"SELECT '\x1b[31m'", TokenScanReason.CONTROL_CHARACTER),
        (b"SELECT 1 -- \x07", TokenScanReason.CONTROL_CHARACTER),
        (b"SELECT\x0b1", TokenScanReason.CONTROL_CHARACTER),
        ("SELECT '​'".encode(), TokenScanReason.CONTROL_CHARACTER),
        ("SELECT 1 -- ‮".encode(), TokenScanReason.CONTROL_CHARACTER),
        ("SELECT a FROM t".encode(), TokenScanReason.CONTROL_CHARACTER),
        ("SELECT a，b FROM t".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        ("SELECT a FROM t WHERE b ＝ 1".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        ("SELECT a FROM t WHERE b = ‘x’".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        ("SELECT a FROM t；".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        ("SELECT　a FROM t".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        ("SELECT a FROM t".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        ("SELECT 订单 FROM t".encode(), TokenScanReason.AMBIGUOUS_PUNCTUATION),
        (b"SELECT 'unterminated", TokenScanReason.UNTERMINATED),
        (b"SELECT `unterminated", TokenScanReason.UNTERMINATED),
        (b"SELECT 1 /* unterminated", TokenScanReason.UNTERMINATED),
        (b"SELECT 'a\\'", TokenScanReason.UNTERMINATED),
    ],
)
def test_rejected_shapes(raw: bytes, reason: TokenScanReason) -> None:
    with pytest.raises(TokenScanError) as caught:
        scan_sql(raw)
    assert caught.value.reason is reason

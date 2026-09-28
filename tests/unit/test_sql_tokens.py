"""quote-aware token 扫描的结构性质（设计 §7.2）。"""

import pytest

from xiaowei_agent.governance.sql_tokens import (
    SQL_MAX_BYTES,
    TokenKind,
    TokenScanError,
    TokenScanReason,
    scan_sql,
)


def _kinds_and_text(raw: bytes) -> list[tuple[TokenKind, str]]:
    scan = scan_sql(raw)
    return [(token.kind, scan.text(token)) for token in scan.tokens]


def test_tokens_cover_words_strings_identifiers_symbols_and_comments() -> None:
    raw = "SELECT `订单`.a, '中文;' FROM db.t -- 注释\nWHERE x = \"y\"".encode()
    assert _kinds_and_text(raw) == [
        (TokenKind.WORD, "SELECT"),
        (TokenKind.QUOTED_IDENTIFIER, "`订单`"),
        (TokenKind.SYMBOL, "."),
        (TokenKind.WORD, "a"),
        (TokenKind.SYMBOL, ","),
        (TokenKind.STRING, "'中文;'"),
        (TokenKind.WORD, "FROM"),
        (TokenKind.WORD, "db"),
        (TokenKind.SYMBOL, "."),
        (TokenKind.WORD, "t"),
        (TokenKind.COMMENT, "-- 注释"),
        (TokenKind.WORD, "WHERE"),
        (TokenKind.WORD, "x"),
        (TokenKind.SYMBOL, "="),
        (TokenKind.STRING, '"y"'),
    ]


def test_every_token_slices_back_to_the_exact_original_bytes() -> None:
    raw = "SELECT '日本' AS `列`, 1 /* 块 */ # 行\nFROM t;".encode()
    scan = scan_sql(raw)
    for token in scan.tokens:
        assert raw[token.start_byte : token.end_byte].decode() == scan.text(token)
    # 多字节字符按字节计：'日本' 的结束位置是字节偏移而不是字符偏移。
    string = next(t for t in scan.tokens if t.kind is TokenKind.STRING)
    assert raw[string.start_byte : string.end_byte] == "'日本'".encode()


def test_escaped_quotes_stay_inside_their_literal() -> None:
    for raw in (b"SELECT 'a\\';b'", b"SELECT 'a'';b'", b'SELECT "a\\";b"', b"SELECT `a``;b`"):
        scan = scan_sql(raw)
        assert [t.kind for t in scan.tokens][-1] in {
            TokenKind.STRING,
            TokenKind.QUOTED_IDENTIFIER,
        }
        assert len(scan.tokens) == 2


def test_statement_tokens_drop_comments_and_one_trailing_semicolon() -> None:
    scan = scan_sql(b"SHOW TABLES ; -- done\n/* bye */")
    assert [scan.text(t) for t in scan.statement_tokens] == ["SHOW", "TABLES"]


def test_line_comments_end_at_either_line_terminator() -> None:
    # 回车也结束行注释：服务端若在 \r 处结束注释，后面的文本必须被当成代码检查。
    with pytest.raises(TokenScanError) as caught:
        scan_sql(b"SELECT 1 -- c\r; DROP TABLE t")
    assert caught.value.reason is TokenScanReason.MULTI_STATEMENT


def test_length_limit_is_in_bytes() -> None:
    assert SQL_MAX_BYTES == 65_536
    body = b"SELECT '" + "中".encode() * 21_842 + b"'"
    assert len(body) == 65_535
    scan_sql(body)
    with pytest.raises(TokenScanError) as caught:
        scan_sql(b"SELECT '" + b"x" * (SQL_MAX_BYTES - 8) + b"'")
    assert caught.value.reason is TokenScanReason.TOO_LONG


def test_scan_error_message_is_only_the_reason_code() -> None:
    with pytest.raises(TokenScanError) as caught:
        scan_sql(b"SELECT 'canary-secret'; DROP TABLE t")
    assert str(caught.value) == TokenScanReason.MULTI_STATEMENT.value
    assert "canary" not in repr(caught.value)

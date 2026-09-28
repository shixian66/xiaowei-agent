"""quote-aware 的 SQL 原文 token 扫描（设计 §7.2）。

AST 之前的一遍单向状态机：识别字符串、quoted identifier、注释与语句边界，并记录每个
token 在**原始 bytes** 中的精确区间。它负责 AST 看不见或解析器可能看错的东西：

- 字符串、quoted identifier 与普通注释里的 ``;`` 不算语句边界；尾随单个 ``;`` 后只剩
  空白或注释时仍是同一条语句；
- 字符串外的 ``/*+``（hint）与 ``/*!``（可执行注释）拒绝；
- 任何位置的控制字符、Unicode format/行分隔字符与 BOM 拒绝；
- 字符串、quoted identifier 与注释之外只允许 ASCII：全角标点、智能引号、全角空格与
  非 ASCII 标识符都拒绝，免得“看起来一样”的字符在解析器与服务端之间产生分歧；
- 字符串外的 ``INTO OUTFILE`` / ``INTO DUMPFILE`` 拒绝。

词法与 StarRocks 一致：``'``/``"`` 字符串支持反斜杠与双写转义，反引号标识符支持双写转义，
``--`` 与 ``#`` 是行注释。行注释在 ``\\r`` 或 ``\\n`` 处结束——若两边对行尾理解不同，按更早
结束处理只会让更多文本被当成代码检查。

不用正则做安全判断；错误只携带闭集原因，消息里不出现任何 SQL 片段。
"""

import itertools
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

SQL_MAX_BYTES: Final[int] = 65_536
"""SQL 原文上限（UTF-8 字节），与 ``sql_artifacts`` 的库内约束一致。"""


class TokenKind(StrEnum):
    WORD = "word"
    QUOTED_IDENTIFIER = "quoted_identifier"
    STRING = "string"
    SYMBOL = "symbol"
    COMMENT = "comment"


class TokenScanReason(StrEnum):
    """扫描拒绝的闭集原因。"""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    INVALID_UTF8 = "invalid_utf8"
    BOM = "bom"
    CONTROL_CHARACTER = "control_character"
    AMBIGUOUS_PUNCTUATION = "ambiguous_punctuation"
    HINT_COMMENT = "hint_comment"
    MULTI_STATEMENT = "multi_statement"
    INTO_OUTFILE = "into_outfile"
    UNTERMINATED = "unterminated"


class TokenScanError(ValueError):
    """原文扫描失败；消息只是闭集原因码。"""

    def __init__(self, reason: TokenScanReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SqlToken:
    """一个 token 在原始 bytes 中的半开区间 ``[start_byte, end_byte)``。"""

    kind: TokenKind
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class TokenScan:
    """扫描结果。``raw`` 不进 repr：它是 SQL 原文。"""

    raw: bytes = field(repr=False)
    tokens: tuple[SqlToken, ...]

    def text(self, token: SqlToken) -> str:
        return self.raw[token.start_byte : token.end_byte].decode()

    @property
    def statement_tokens(self) -> tuple[SqlToken, ...]:
        """去掉注释与尾随 ``;`` 后的语句 token。扫描已保证 ``;`` 至多一个且位于末尾。"""
        significant = tuple(t for t in self.tokens if t.kind is not TokenKind.COMMENT)
        if significant and self._is_semicolon(significant[-1]):
            return significant[:-1]
        return significant

    def _is_semicolon(self, token: SqlToken) -> bool:
        return token.kind is TokenKind.SYMBOL and self.raw[token.start_byte] == 0x3B


_WHITESPACE: Final[frozenset[str]] = frozenset(" \t\r\n")
_LINE_END: Final[frozenset[str]] = frozenset("\r\n")
_WORD_EXTRA: Final[frozenset[str]] = frozenset("_$")
_REJECTED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cf", "Zl", "Zp"})
_FILE_TARGETS: Final[frozenset[str]] = frozenset({"OUTFILE", "DUMPFILE"})


def _check_char(char: str) -> None:
    if char == "﻿":
        raise TokenScanError(TokenScanReason.BOM)
    category = unicodedata.category(char)
    if (category == "Cc" and char not in _WHITESPACE) or category in _REJECTED_CATEGORIES:
        raise TokenScanError(TokenScanReason.CONTROL_CHARACTER)


def _is_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in _WORD_EXTRA)


class _Scanner:
    """单遍扫描器：每个字符只被消费一次，并在消费时做字符级检查。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._byte = 0
        self.tokens: list[SqlToken] = []

    def _peek(self, ahead: int = 0) -> str:
        index = self._pos + ahead
        return self._text[index] if index < len(self._text) else ""

    def _advance(self) -> str:
        char = self._text[self._pos]
        _check_char(char)
        self._pos += 1
        self._byte += len(char.encode())
        return char

    def run(self) -> None:
        while self._pos < len(self._text):
            char = self._peek()
            start = self._byte
            if char in _WHITESPACE:
                self._advance()
                continue
            if (char == "-" and self._peek(1) == "-") or char == "#":
                self._line_comment()
                kind = TokenKind.COMMENT
            elif char == "/" and self._peek(1) == "*":
                self._block_comment()
                kind = TokenKind.COMMENT
            elif char in "'\"":
                self._quoted(char, backslash=True)
                kind = TokenKind.STRING
            elif char == "`":
                self._quoted(char, backslash=False)
                kind = TokenKind.QUOTED_IDENTIFIER
            elif _is_word_char(char):
                while _is_word_char(self._peek()):
                    self._advance()
                kind = TokenKind.WORD
            elif char.isascii():
                self._advance()
                kind = TokenKind.SYMBOL
            else:
                # 先做字符级检查：零宽/控制字符报更准确的原因。
                _check_char(char)
                raise TokenScanError(TokenScanReason.AMBIGUOUS_PUNCTUATION)
            self.tokens.append(SqlToken(kind=kind, start_byte=start, end_byte=self._byte))

    def _line_comment(self) -> None:
        while self._pos < len(self._text) and self._peek() not in _LINE_END:
            self._advance()

    def _block_comment(self) -> None:
        if self._peek(2) in {"+", "!"}:
            raise TokenScanError(TokenScanReason.HINT_COMMENT)
        self._advance()
        self._advance()
        while self._pos < len(self._text):
            if self._peek() == "*" and self._peek(1) == "/":
                self._advance()
                self._advance()
                return
            self._advance()
        raise TokenScanError(TokenScanReason.UNTERMINATED)

    def _quoted(self, quote: str, *, backslash: bool) -> None:
        self._advance()
        while self._pos < len(self._text):
            char = self._advance()
            if backslash and char == "\\":
                if self._pos >= len(self._text):
                    break
                self._advance()
            elif char == quote:
                if self._peek() != quote:
                    return
                self._advance()
        raise TokenScanError(TokenScanReason.UNTERMINATED)


def scan_sql(raw: bytes) -> TokenScan:
    """扫描一条 SQL 原文。

    :raises TokenScanError: 超长、非法 UTF-8、BOM、控制字符、歧义字符、hint、未闭合的
        引号或注释、多语句、空语句或字符串外的 ``INTO OUTFILE``。
    """
    if len(raw) > SQL_MAX_BYTES:
        raise TokenScanError(TokenScanReason.TOO_LONG)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TokenScanError(TokenScanReason.INVALID_UTF8) from None
    if "﻿" in text:
        raise TokenScanError(TokenScanReason.BOM)
    scanner = _Scanner(text)
    scanner.run()
    scan = TokenScan(raw=raw, tokens=tuple(scanner.tokens))
    significant = [t for t in scan.tokens if t.kind is not TokenKind.COMMENT]
    for index, token in enumerate(significant):
        if scan._is_semicolon(token) and index != len(significant) - 1:
            raise TokenScanError(TokenScanReason.MULTI_STATEMENT)
    words = [scan.text(t).upper() if t.kind is TokenKind.WORD else "" for t in significant]
    for current, following in itertools.pairwise(words):
        if current == "INTO" and following in _FILE_TARGETS:
            raise TokenScanError(TokenScanReason.INTO_OUTFILE)
    if not scan.statement_tokens:
        raise TokenScanError(TokenScanReason.EMPTY)
    return scan

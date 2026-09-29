"""确定性 SQL 消息识别与嵌入 SQL 检测（设计 §5.1）。

``classify_sql_text`` 把一条消息分成三档（设计 §5.1，负责人 2026-09-28 决定），依据是唯一的
识别登记表 ``governance.sql_statements``（覆盖 StarRocks 文档全部顶层 SQL 语句与 MySQL 兼容语句）：

- **SQL**：明确标记为 ``sql`` 的代码块；或命中只读语句清单；或命中登记表某种语句的签名且整条
  符合它的完整形状（文法完整匹配，SELECT/WITH/UPDATE 交 sqlglot 判断完整有效）。多语句按第一条
  判断，hint 与 ``INTO OUTFILE`` 只出现在 SQL 里。识别出的 SQL 保存为 SqlArtifact，永不进入模型；
  是否支持、是否只读由 SQLGuard 判断（清单外的已知语句回复“暂未支持”）。
- **像 SQL（SQL_LIKE）**：命中签名但不符合完整形状（如 “show data for yesterday”、
  “create table for this report”、未闭合的 ``SELECT 'x``）；或签名都不命中、首条语句却能被 sqlglot
  完整解析成真正的查询（``SELECT TRUE AND TRUE``、``SELECT NULL``、一元表达式、CASE，以及分不开的
  “select this”）——签名补不完，这里 fail-closed。提交边界在写任何事实前拒绝，原文不落库、不进
  模型，固定提示放入 sql 代码块重发。
- **对话**：首词不是登记的语句关键字，或签名都不命中且不是查询（如 “show me the slow queries”、
  “create a dashboard”、“analyze this”），或在字符串与注释之外出现中文等自然语言文字。

sqlglot 会把不少英文短句降级为 ``Command`` 或宽松解析成语句，所以既不把 ``Command`` 一律当 SQL，
也不只看能否解析。判定在一份**判定副本**上进行：复制粘贴常带进来的智能引号、全角字符（NFKC）
与不可见的控制/格式字符先归一化或去掉。保存与执行的永远是原文，SQLGuard 仍按原文拒绝这些字符。

``contains_embedded_sql`` 是产品护栏，不是安全边界：它让“夹带 SQL 的对话”以固定文案拒绝，
不执行任何 capability。漏判时消息按现有流程处理，现有能力只执行自己的模板 SQL。

两者都是纯函数，不调用模型、不做 I/O；同一输入永远得到同一结论。
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from xiaowei_agent.contracts import SqlTextKind
from xiaowei_agent.governance.readonly_statements import match_statement
from xiaowei_agent.governance.sql_statements import (
    SQL_STATEMENT_FORMS,
    SqlStatementForm,
    lenient_tokens,
    shape_matches,
    signature_matches,
)
from xiaowei_agent.governance.sql_tokens import (
    SQL_MAX_BYTES,
    TokenScanError,
    TokenScanReason,
    scan_sql,
)
from xiaowei_agent.governance.sqlguard import (
    SqlParseShape,
    parses_as_complete_statements,
    parses_as_query,
    sql_parse_shape,
)

__all__ = [
    "SQL_STATEMENT_KEYWORDS",
    "SqlMessage",
    "SqlTextKind",
    "classify_sql_text",
    "contains_embedded_sql",
    "recognize_sql_message",
]

SQL_STATEMENT_KEYWORDS: Final[frozenset[str]] = frozenset(
    form.keyword for form in SQL_STATEMENT_FORMS
)
"""SQL 语句关键字闭集，从识别登记表派生；嵌入 SQL 检测用它找候选片段。不授予任何权限。"""

_FORMS_BY_KEYWORD: Final[dict[str, tuple[SqlStatementForm, ...]]] = {
    keyword: tuple(form for form in SQL_STATEMENT_FORMS if form.keyword == keyword)
    for keyword in SQL_STATEMENT_KEYWORDS
}


_UNMISTAKABLE_SCAN_FAILURES: Final[frozenset[TokenScanReason]] = frozenset(
    {TokenScanReason.HINT_COMMENT, TokenScanReason.INTO_OUTFILE}
)
"""sqlglot 解析不了、但只会出现在 SQL 里的语法：命中签名即识别，交 SQLGuard 明确拒绝。"""

_VALID_PARSE_SHAPES: Final[frozenset[SqlParseShape]] = frozenset(
    {SqlParseShape.STATEMENTS, SqlParseShape.TOO_COMPLEX}
)
"""完整有效的 SQL：解析为语句，或因嵌套过深放弃。``Command`` 只是“关键字 + 原文”，不算。"""

_LOOKALIKE_QUOTES: Final[dict[int, str]] = {
    ord(char): "'" for char in "\u2018\u2019\u201a\u201b"
} | {ord(char): '"' for char in "\u201c\u201d\u201e\u201f"}

_WHOLE_FENCE: Final = re.compile(
    r"\A```(?P<info>[^\n`]*)\n(?P<body>.*?)\n?```\Z", re.DOTALL
)
_FENCE_BLOCK: Final = re.compile(r"^```[^\n`]*\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE)
_PARAGRAPH_BREAK: Final = re.compile(r"\n[ \t]*\n")
_LEADING_WORD: Final = re.compile(r"\A\s*([A-Za-z]+)(?![A-Za-z0-9_])")
_KEYWORD: Final = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(sorted(SQL_STATEMENT_KEYWORDS))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FRAGMENT_TOKEN: Final = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")
_MIN_FREE_TEXT_TOKENS: Final[int] = 3
"""正文外片段至少 3 个 token：sqlglot 会把 “delete prod”“begin”“kill it” 这类普通英文
解析成语句；真实夹带的 SQL 几乎都更长。代码块正文是用户有意的载体，不设此下限。"""
_MAX_EMBEDDED_CANDIDATES: Final[int] = 64
"""嵌入检测最多解析的候选数；护栏不是安全边界，超出部分按未命中处理。"""


@dataclass(frozen=True, slots=True)
class SqlMessage:
    """识别出的 SQL 形状文本与它的分档。

    只有 ``kind`` 为 ``SQL`` 的才保存为 SqlArtifact（就是 ``sql`` 的 UTF-8 bytes）；``SQL_LIKE``
    只用来在写任何事实前拒绝。
    """

    sql: str
    kind: SqlTextKind

    @property
    def sql_bytes(self) -> bytes:
        return self.sql.encode("utf-8")


def _unwrap(stripped: str) -> tuple[str, bool]:
    """返回正文，以及它是否来自明确标记为 ``sql`` 的代码块。"""
    fenced = _WHOLE_FENCE.fullmatch(stripped)
    if fenced is None:
        return stripped, False
    body = fenced.group("body")
    if "```" in body:
        return "", False
    return body.strip(), fenced.group("info").strip().lower() == "sql"


def _leading_keyword(text: str) -> bool:
    match = _LEADING_WORD.match(text)
    return match is not None and match.group(1).upper() in SQL_STATEMENT_KEYWORDS


def _judgement_copy(body: str) -> str:
    """判定副本：智能引号换成 ASCII 引号，NFKC 折叠全角字符，去掉控制与格式字符。

    只用于判断 SQL 形状；中文等自然语言文字在 NFKC 后仍不是 ASCII，扫描照样拒绝。
    """
    folded = unicodedata.normalize("NFKC", body.translate(_LOOKALIKE_QUOTES))
    return "".join(
        char
        for char in folded
        if char in "\t\r\n"
        or unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"}
    )


def _first_statement(probe: str) -> str:
    for token in lenient_tokens(probe):
        if token.kind == "symbol" and token.text == ";":
            return probe[: token.start]
    return probe


def _unsigned_query(probe: str) -> bool:
    """签名都不命中时，首条语句能否被完整解析成真正的查询。

    字符串与注释之外出现中文等自然语言文字的仍是混合消息，交嵌入 SQL 检测。
    """
    first = _first_statement(probe)
    try:
        scan_sql(first.encode("utf-8"))
    except TokenScanError as exc:
        if exc.reason is TokenScanReason.AMBIGUOUS_PUNCTUATION:
            return False
    return parses_as_query(first)


def _kind(probe: str) -> SqlTextKind:
    tokens = lenient_tokens(probe)
    if not tokens or tokens[0].kind != "word":
        return SqlTextKind.CONVERSATION
    forms = _FORMS_BY_KEYWORD.get(tokens[0].text, ())
    if not forms:
        return SqlTextKind.CONVERSATION
    signed = [form for form in forms if signature_matches(form.signature, tokens)]
    if not signed:
        # 签名补不完（SELECT TRUE、SELECT NULL、一元表达式、CASE…），也分不开 “select this”：
        # 能解析成真正查询的 fail-closed 为“像 SQL”，绝不进模型。
        return SqlTextKind.SQL_LIKE if _unsigned_query(probe) else SqlTextKind.CONVERSATION
    try:
        scan = scan_sql(probe.encode("utf-8"))
    except TokenScanError as exc:
        if exc.reason in _UNMISTAKABLE_SCAN_FAILURES:
            return SqlTextKind.SQL
        if exc.reason is TokenScanReason.MULTI_STATEMENT:
            # 多语句按第一条判断：是 SQL 就交 SQLGuard 以多语句拒绝。
            return _kind(_first_statement(probe))
        if exc.reason is TokenScanReason.AMBIGUOUS_PUNCTUATION:
            # 字符串与注释之外有中文等自然语言文字：混合消息，交嵌入 SQL 检测。
            return SqlTextKind.CONVERSATION
        return SqlTextKind.SQL_LIKE
    if match_statement(scan) is not None:
        return SqlTextKind.SQL
    for form in signed:
        complete = shape_matches(form.shape, scan)
        if complete is None:
            complete = sql_parse_shape(probe) in _VALID_PARSE_SHAPES
        if complete:
            return SqlTextKind.SQL
    return SqlTextKind.SQL_LIKE


def _classify(text: str) -> tuple[SqlTextKind, str]:
    body, declared_sql = _unwrap(text.strip())
    if not body or len(body.encode("utf-8")) > SQL_MAX_BYTES:
        return SqlTextKind.CONVERSATION, body
    if declared_sql:
        return SqlTextKind.SQL, body
    return _kind(_judgement_copy(body)), body


def classify_sql_text(text: str) -> SqlTextKind:
    """按设计 §5.1 把消息分成 SQL / 像 SQL / 对话三档；纯函数，同一输入同一结论。"""
    return _classify(text)[0]


def recognize_sql_message(text: str) -> SqlMessage | None:
    """识别 SQL 形状的消息；对话返回 ``None``。

    “像 SQL”也返回（``kind`` 为 ``SQL_LIKE``），不和对话混成 ``None``：提交边界必须据此在写任何
    事实前拒绝，否则原文会落进没有 24 小时清理的普通对话提交。返回的正文是原文，不是判定副本。
    """
    kind, body = _classify(text)
    return None if kind is SqlTextKind.CONVERSATION else SqlMessage(sql=body, kind=kind)


def _ascii_prefix(fragment: str) -> str:
    """片段截到第一个非 ASCII 字符为止：SQL 之后接中文说明是常见写法。"""
    for index, character in enumerate(fragment):
        if not character.isascii():
            return fragment[:index]
    return fragment


def _is_sql_fragment(fragment: str, *, min_tokens: int) -> bool:
    candidate = _ascii_prefix(fragment).strip()
    return (
        len(_FRAGMENT_TOKEN.findall(candidate)) >= min_tokens
        and _leading_keyword(candidate)
        and parses_as_complete_statements(candidate)
    )


def _candidates(text: str) -> list[tuple[str, int]]:
    """每个代码块正文，以及正文外每个以语句关键字开头、到段落末尾的文本。"""
    candidates: list[tuple[str, int]] = []
    outside: list[str] = []
    cursor = 0
    for block in _FENCE_BLOCK.finditer(text):
        candidates.append((block.group("body"), 1))
        outside.append(text[cursor : block.start()])
        cursor = block.end()
    outside.append(text[cursor:])
    for part in outside:
        for paragraph in _PARAGRAPH_BREAK.split(part):
            candidates.extend(
                (paragraph[keyword.start() :], _MIN_FREE_TEXT_TOKENS)
                for keyword in _KEYWORD.finditer(paragraph)
            )
    return candidates


def contains_embedded_sql(text: str) -> bool:
    """消息中是否夹带可解析的 SQL 语句；代码块只是载体，非 SQL 代码块不命中。"""
    return any(
        _is_sql_fragment(candidate, min_tokens=min_tokens)
        for candidate, min_tokens in _candidates(text)[:_MAX_EMBEDDED_CANDIDATES]
    )

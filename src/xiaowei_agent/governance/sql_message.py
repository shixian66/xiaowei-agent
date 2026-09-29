"""确定性 SQL 消息识别与嵌入 SQL 检测（设计 §5.1）。

``recognize_sql_message`` 只给两种结论：SQL 或对话（负责人 2026-09-29 决定：像 SQL 的就是 SQL）。
依据是唯一的识别登记表 ``governance.sql_statements``（覆盖 StarRocks 文档全部顶层 SQL 语句与 MySQL
兼容语句）：

- **SQL**：明确标记为 ``sql`` 的代码块；或命中登记表某种语句的签名（写不完整、写错也算，例如
  “show data for yesterday”、未闭合的 ``SELECT 'x``）；或签名都不命中、首条
  语句却能被 sqlglot 完整解析成真正的查询（``SELECT TRUE AND TRUE``、``SELECT NULL``、一元表达式、
  CASE）。识别出的 SQL 保存为 SqlArtifact，永不进入模型；是否支持、是否只读由 SQLGuard 判断，
  执行失败把报错返回给用户。
- **对话**：签名都不命中也不是查询（如 “show me the slow queries”、“create a dashboard”、
  “analyze this”、“(hello)”），或在字符串与注释之外出现中文等自然语言文字（混合消息，
  交嵌入 SQL 检测）。

判定在一份**判定副本**上进行：复制粘贴常带进来的智能引号、全角字符（NFKC）与不可见的控制/格式
字符先归一化或去掉。保存与执行的永远是原文，SQLGuard 仍按原文拒绝这些字符。

``contains_embedded_sql`` 是产品护栏，不是安全边界：它让“夹带 SQL 的对话”以固定文案拒绝，
不执行任何 capability。漏判时消息按现有流程处理，现有能力只执行自己的模板 SQL。

两者都是纯函数，不调用模型、不做 I/O；同一输入永远得到同一结论。
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from xiaowei_agent.governance.sql_statements import (
    SQL_STATEMENT_FORMS,
    SqlStatementForm,
    lenient_tokens,
    signature_matches,
)
from xiaowei_agent.governance.sql_tokens import (
    SQL_MAX_BYTES,
    TokenScanError,
    TokenScanReason,
    scan_sql,
)
from xiaowei_agent.governance.sqlguard import (
    parses_as_complete_statements,
    parses_as_query,
)

__all__ = [
    "SQL_STATEMENT_KEYWORDS",
    "SqlMessage",
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
    """识别出的 SQL 语句文本；保存为 SqlArtifact 的就是它的 UTF-8 bytes。"""

    sql: str

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


def _is_sql(probe: str) -> bool:
    tokens = lenient_tokens(probe)
    if not tokens:
        return False
    try:
        scan_sql(probe.encode("utf-8"))
    except TokenScanError as exc:
        first = _first_statement(probe)
        if exc.reason is TokenScanReason.MULTI_STATEMENT and first != probe:
            # 多语句按第一条判断：是 SQL 就交 SQLGuard 以多语句拒绝。
            return _is_sql(first)
        if exc.reason is TokenScanReason.AMBIGUOUS_PUNCTUATION:
            # 字符串与注释之外有中文等自然语言文字：混合消息，交嵌入 SQL 检测。
            return False
    forms = _FORMS_BY_KEYWORD.get(tokens[0].text, ()) if tokens[0].kind == "word" else ()
    if any(signature_matches(form.signature, tokens) for form in forms):
        return True
    # 三个条件是“或”：签名补不完（SELECT TRUE、一元表达式、CASE…）或首 token 不是登记关键字
    # （括号查询、FROM 开头）时，首条语句能解析成真正查询的也是 SQL。
    return parses_as_query(_first_statement(probe))


def recognize_sql_message(text: str) -> SqlMessage | None:
    """识别纯 SQL 消息；对话返回 ``None``。返回的是原文正文，不是判定副本。"""
    body, declared_sql = _unwrap(text.strip())
    if not body or len(body.encode("utf-8")) > SQL_MAX_BYTES:
        return None
    if declared_sql or _is_sql(_judgement_copy(body)):
        return SqlMessage(sql=body)
    return None


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

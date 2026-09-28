"""确定性 SQL 消息识别与嵌入 SQL 检测（设计 §5.1）。

``recognize_sql_message`` 决定一条渠道消息是否是“纯 SQL 消息”：只有它识别出的 SQL 才会被
保存为 SqlArtifact、进而可能执行。三条规则同时满足才算：

1. 去掉首尾空白；整条消息恰好是一个 fenced code block 时只取块内正文；
2. 首个语句 token 属于闭集 SQL 语句关键字（读、写与会话语句都在内——写语句也要被识别，
   以便明确回复“只允许只读查询”，而不是当成聊天）；
3. 恰好一条完整语句：sqlglot 完整解析为非 ``Command`` 语句，或命中代码内只读语句清单。
   多语句、hint 与 ``INTO OUTFILE`` 虽不能通过 token 扫描，但语法上仍是 SQL，照样识别，
   由 SQLGuard 给出明确拒绝。

``contains_embedded_sql`` 是产品护栏，不是安全边界：它让“夹带 SQL 的对话”以固定文案拒绝，
不执行任何 capability。漏判时消息按现有流程处理，现有能力只执行自己的模板 SQL。

两者都是纯函数，不调用模型、不做 I/O；同一输入永远得到同一结论。
"""

import re
from dataclasses import dataclass
from typing import Final

from xiaowei_agent.governance.readonly_statements import match_statement
from xiaowei_agent.governance.sql_tokens import (
    SQL_MAX_BYTES,
    TokenKind,
    TokenScanError,
    TokenScanReason,
    scan_sql,
)
from xiaowei_agent.governance.sqlguard import parses_as_complete_statements

__all__ = [
    "SQL_STATEMENT_KEYWORDS",
    "SqlMessage",
    "contains_embedded_sql",
    "recognize_sql_message",
]

SQL_STATEMENT_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # 读
        "SELECT",
        "WITH",
        "SHOW",
        "DESC",
        "DESCRIBE",
        "EXPLAIN",
        "ADMIN",
        "ANALYZE",
        # 写、DDL、权限与会话
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "REPLACE",
        "UPSERT",
        "CREATE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "RENAME",
        "GRANT",
        "REVOKE",
        "SET",
        "USE",
        "KILL",
        "LOAD",
        "EXPORT",
        "SUBMIT",
        "CANCEL",
        "BEGIN",
        "START",
        "COMMIT",
        "ROLLBACK",
        "LOCK",
        "UNLOCK",
        "REFRESH",
        "RECOVER",
        "BACKUP",
        "RESTORE",
        "INSTALL",
        "UNINSTALL",
        "PAUSE",
        "RESUME",
        "STOP",
        "CALL",
    }
)
"""识别 SQL 消息的首 token 闭集（设计 §5.1 第 2 条）；只影响“是不是 SQL”，不授予任何权限。"""

_SQL_SHAPED_SCAN_FAILURES: Final[frozenset[TokenScanReason]] = frozenset(
    {
        TokenScanReason.MULTI_STATEMENT,
        TokenScanReason.HINT_COMMENT,
        TokenScanReason.INTO_OUTFILE,
    }
)
"""扫描拒绝但语法上仍是 SQL 的原因：识别为 SQL，交 SQLGuard 明确拒绝。"""

_WHOLE_FENCE: Final = re.compile(r"\A```[^\n`]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)
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


def _unwrap(stripped: str) -> str:
    fenced = _WHOLE_FENCE.fullmatch(stripped)
    if fenced is None:
        return stripped
    body = fenced.group("body")
    return "" if "```" in body else body.strip()


def _leading_keyword(text: str) -> bool:
    match = _LEADING_WORD.match(text)
    return match is not None and match.group(1).upper() in SQL_STATEMENT_KEYWORDS


def recognize_sql_message(text: str) -> SqlMessage | None:
    """按三条规则识别纯 SQL 消息；不是 SQL 时返回 ``None``（按普通对话处理）。"""
    body = _unwrap(text.strip())
    raw = body.encode("utf-8")
    if not body or len(raw) > SQL_MAX_BYTES:
        return None
    try:
        scan = scan_sql(raw)
    except TokenScanError as exc:
        if exc.reason not in _SQL_SHAPED_SCAN_FAILURES or not _leading_keyword(body):
            return None
        # sqlglot 不认识 INTO OUTFILE；扫描器已在语句 token 中看到它，这本身就是 SQL 形状。
        if exc.reason is TokenScanReason.INTO_OUTFILE or parses_as_complete_statements(body):
            return SqlMessage(sql=body)
        return None
    first = scan.statement_tokens[0]
    if first.kind is not TokenKind.WORD or scan.text(first).upper() not in (
        SQL_STATEMENT_KEYWORDS
    ):
        return None
    if parses_as_complete_statements(body) or match_statement(scan) is not None:
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

"""确定性 SQL 消息识别与嵌入 SQL 检测（设计 §5.1）。

``recognize_sql_message`` 决定一条消息是否是“纯 SQL 消息”：识别出的 SQL 保存为 SqlArtifact，
永不进入模型；其余消息才可能按普通对话进入模型。它只判断 **是不是 SQL**，不判断是否支持、
是否安全——那是 SQLGuard 的事。去掉首尾空白、整条消息恰好是一个 fenced code block 时只取块内
正文后，满足下列任一条就是 SQL：

1. 代码块明确标记为 ``sql``：用户已声明这是 SQL，内容交 SQLGuard 判断；
2. 跳过前置注释与 hint 后，前两个词命中已知 StarRocks 语句族（``SQL_STATEMENT_FAMILIES``，
   如 ``SHOW USERS``、``ADMIN SHOW``、``CREATE TABLE``），或命中代码内只读语句清单——清单外的
   已知语句（包括 sqlglot 解析不了的）照样识别，由 SQLGuard 回复“暂未支持”；
3. 首词属于开放语句族（``SELECT``、``WITH``、``UPDATE`` 等，第二个词无法枚举）且整条是完整
   有效的 SQL：sqlglot 解析为语句或因嵌套过深放弃；``INTO OUTFILE`` 与 hint 虽不能通过 token
   扫描，也算。

sqlglot 会把 “show me the slow queries”“create a dashboard” 降级为 ``Command``、把
“analyze this”“delete prod” 宽松解析成语句，所以既不能把 ``Command`` 一律当 SQL，也不能只看
能否解析；以 SQL 关键字开头的自然语言都按对话处理。多语句、hint 与 ``INTO OUTFILE`` 只要满足
上面某条就识别，由 SQLGuard 明确拒绝。

判定在一份**判定副本**上进行：复制粘贴常带进来的智能引号、全角字符（NFKC）与不可见的
控制/格式字符先归一化或去掉。保存与执行的永远是原文，SQLGuard 仍按原文拒绝这些字符。
字符串、quoted identifier 与注释之外出现中文等自然语言文字的消息不是 SQL 消息（第 1 条除外）。

``contains_embedded_sql`` 是产品护栏，不是安全边界：它让“夹带 SQL 的对话”以固定文案拒绝，
不执行任何 capability。漏判时消息按现有流程处理，现有能力只执行自己的模板 SQL。

两者都是纯函数，不调用模型、不做 I/O；同一输入永远得到同一结论。
"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from xiaowei_agent.governance.readonly_statements import match_statement
from xiaowei_agent.governance.sql_tokens import (
    SQL_MAX_BYTES,
    TokenScanError,
    TokenScanReason,
    scan_sql,
)
from xiaowei_agent.governance.sqlguard import (
    SqlParseShape,
    parses_as_complete_statements,
    sql_parse_shape,
)

__all__ = [
    "SQL_STATEMENT_FAMILIES",
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
"""SQL 语句关键字闭集：嵌入 SQL 检测用它找候选片段；只影响“是不是 SQL”，不授予任何权限。"""

_NUMBER: Final = "#NUMBER"
"""第二个 token 是整数（``KILL 123``）。"""
_END: Final = ""
"""关键字之后就结束（``BEGIN``、``COMMIT;``）。"""
_OTHER: Final = "#OTHER"
"""第二个 token 是符号等，不属于任何已知语句族。"""

_DDL_OBJECTS: Final = frozenset(
    {
        "ANALYZE", "CATALOG", "DATABASE", "DICTIONARY", "EXTERNAL", "FILE", "FUNCTION",
        "GLOBAL", "INDEX", "MATERIALIZED", "OR", "PIPE", "REPOSITORY", "RESOURCE", "ROLE",
        "ROUTINE", "SCHEMA", "SECURITY", "STATS", "STORAGE", "SYSTEM", "TABLE", "TASK",
        "TEMPORARY", "USER", "VIEW", "WAREHOUSE",
    }
)
_SHOW_OBJECTS: Final = frozenset(
    {
        "ALTER", "ANALYZE", "AUTHENTICATION", "BACKENDS", "BACKUP", "BROKER", "BUILTIN",
        "CATALOGS", "CHARACTER", "CHARSET", "COLLATION", "COLUMNS", "COMPACTIONS", "COMPUTE",
        "CREATE", "DATA", "DATABASES", "DATACACHE", "DELETE", "DYNAMIC", "ENGINES", "ERRORS",
        "EVENTS", "EXPORT", "EXTERNAL", "FIELDS", "FILE", "FRONTENDS", "FULL", "FUNCTIONS",
        "GLOBAL", "GRANTS", "HISTOGRAM", "INDEX", "INDEXES", "KEYS", "LOAD", "MATERIALIZED",
        "OPEN", "PARTITIONS", "PIPES", "PLUGINS", "PRIVILEGES", "PROC", "PROCEDURE",
        "PROCESSLIST", "PROFILELIST", "PROPERTIES", "PROPERTY", "REPOSITORIES", "RESOURCE",
        "RESOURCES", "RESTORE", "ROLES", "ROUTINE", "RUNNING", "SCHEMAS", "SESSION",
        "SNAPSHOT", "SQLBLACKLIST", "STATS", "STATUS", "STORAGE", "STREAM", "TABLE", "TABLES",
        "TABLET", "TABLETS", "TEMPORARY", "TRANSACTION", "TRIGGERS", "USERS", "VARIABLES",
        "WAREHOUSES", "WARNINGS", "WHITELIST",
    }
)
_TRANSACTION_TAIL: Final = frozenset({_END, "WORK"})

SQL_STATEMENT_FAMILIES: Final[Mapping[str, frozenset[str] | None]] = MappingProxyType(
    {
        # 开放语句族：第二个词无法枚举，整条必须是完整有效的 SQL。
        "SELECT": None,
        "WITH": None,
        "UPDATE": None,
        "SET": None,
        "GRANT": None,
        "REVOKE": None,
        "DESC": None,
        "DESCRIBE": None,
        "USE": None,
        # 已知 StarRocks 语句族：关键字 + 第二个词，sqlglot 能否解析都识别。
        "SHOW": _SHOW_OBJECTS,
        "CREATE": _DDL_OBJECTS,
        "DROP": _DDL_OBJECTS,
        "ALTER": _DDL_OBJECTS,
        "EXPLAIN": frozenset(
            {"SELECT", "WITH", "VERBOSE", "COSTS", "LOGICAL", "ANALYZE", "INSERT",
             "UPDATE", "DELETE"}
        ),
        "ADMIN": frozenset({"SHOW", "SET", "REPAIR", "CANCEL", "CHECK", "COMPACT", "EXECUTE"}),
        "ANALYZE": frozenset({"TABLE", "FULL", "SAMPLE", "PROFILE"}),
        "KILL": frozenset({"QUERY", "CONNECTION", _NUMBER}),
        "INSERT": frozenset({"INTO", "OVERWRITE"}),
        "DELETE": frozenset({"FROM"}),
        "REPLACE": frozenset({"INTO"}),
        "TRUNCATE": frozenset({"TABLE"}),
        "LOCK": frozenset({"TABLES"}),
        "UNLOCK": frozenset({"TABLES"}),
        "LOAD": frozenset({"LABEL"}),
        "EXPORT": frozenset({"TABLE"}),
        "SUBMIT": frozenset({"TASK"}),
        "CANCEL": frozenset(
            {"LOAD", "EXPORT", "ALTER", "BACKUP", "RESTORE", "REFRESH", "DECOMMISSION"}
        ),
        "REFRESH": frozenset({"MATERIALIZED", "EXTERNAL"}),
        "RECOVER": frozenset({"DATABASE", "TABLE", "PARTITION"}),
        "BACKUP": frozenset({"SNAPSHOT"}),
        "RESTORE": frozenset({"SNAPSHOT"}),
        "INSTALL": frozenset({"PLUGIN"}),
        "UNINSTALL": frozenset({"PLUGIN"}),
        "PAUSE": frozenset({"ROUTINE"}),
        "RESUME": frozenset({"ROUTINE"}),
        "STOP": frozenset({"ROUTINE"}),
        "BEGIN": _TRANSACTION_TAIL,
        "COMMIT": _TRANSACTION_TAIL,
        "ROLLBACK": _TRANSACTION_TAIL,
        "START": frozenset({"TRANSACTION"}),
    }
)
"""识别 SQL 消息的语句族（设计 §5.1）：首词 → 已知第二个词，``None`` 表示开放语句族。
读、写与会话语句都在内——写语句也要被识别，以便明确回复“只允许只读查询”，而不是当成聊天。"""

_SQL_SHAPED_SCAN_FAILURES: Final[frozenset[TokenScanReason]] = frozenset(
    {
        TokenScanReason.MULTI_STATEMENT,
        TokenScanReason.HINT_COMMENT,
        TokenScanReason.INTO_OUTFILE,
    }
)
"""扫描拒绝但语法上仍可能是 SQL 的原因：满足语句族规则时识别，交 SQLGuard 明确拒绝。"""

_UNMISTAKABLE_SCAN_FAILURES: Final[frozenset[TokenScanReason]] = frozenset(
    {TokenScanReason.HINT_COMMENT, TokenScanReason.INTO_OUTFILE}
)
"""sqlglot 解析不了、但只会出现在 SQL 里的语法；开放语句族据此识别。"""

_VALID_PARSE_SHAPES: Final[frozenset[SqlParseShape]] = frozenset(
    {SqlParseShape.STATEMENTS, SqlParseShape.TOO_COMPLEX}
)
"""完整有效的 SQL：解析为语句，或因嵌套过深放弃。``Command`` 只是“关键字 + 原文”，不算。"""

_LOOKALIKE_QUOTES: Final[dict[int, str]] = {
    ord(char): "'" for char in "\u2018\u2019\u201a\u201b"
} | {ord(char): '"' for char in "\u201c\u201d\u201e\u201f"}
_LEADING_COMMENT: Final = re.compile(r"\A\s*(?:(?:--|#)[^\r\n]*|/\*.*?\*/)", re.DOTALL)

_WHOLE_FENCE: Final = re.compile(
    r"\A```(?P<info>[^\n`]*)\n(?P<body>.*?)\n?```\Z", re.DOTALL
)
_FENCE_BLOCK: Final = re.compile(r"^```[^\n`]*\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE)
_PARAGRAPH_BREAK: Final = re.compile(r"\n[ \t]*\n")
_LEADING_WORD: Final = re.compile(r"\A\s*([A-Za-z]+)(?![A-Za-z0-9_])")
_FOLLOWING_TOKEN: Final = re.compile(
    r"\A\s*(?:(?P<word>[A-Za-z]+)|(?P<number>[0-9]+))(?![A-Za-z0-9_])"
)
_STATEMENT_END: Final = re.compile(r"\A\s*(?:;\s*)?\Z")
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


def _without_leading_comments(text: str) -> str:
    while (comment := _LEADING_COMMENT.match(text)) is not None:
        text = text[comment.end() :]
    return text


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


def _statement_head(text: str) -> tuple[str, str] | None:
    """跳过注释与 hint 后的首词与第二个 token（整数记为 ``#NUMBER``，语句结束记为空串）。"""
    text = _without_leading_comments(text)
    first = _LEADING_WORD.match(text)
    if first is None:
        return None
    rest = _without_leading_comments(text[first.end() :])
    if _STATEMENT_END.match(rest) is not None:
        return first.group(1).upper(), _END
    following = _FOLLOWING_TOKEN.match(rest)
    if following is None:
        return first.group(1).upper(), _OTHER
    word = following.group("word")
    return first.group(1).upper(), _NUMBER if word is None else word.upper()


def _is_sql(probe: str) -> bool:
    head = _statement_head(probe)
    if head is None or head[0] not in SQL_STATEMENT_FAMILIES:
        return False
    keyword, following = head
    try:
        scan = scan_sql(probe.encode("utf-8"))
    except TokenScanError as exc:
        if exc.reason not in _SQL_SHAPED_SCAN_FAILURES:
            return False
        scan_failure: TokenScanReason | None = exc.reason
    else:
        if match_statement(scan) is not None:
            return True
        scan_failure = None
    followers = SQL_STATEMENT_FAMILIES[keyword]
    if followers is not None:
        return following in followers
    return (
        scan_failure in _UNMISTAKABLE_SCAN_FAILURES
        or sql_parse_shape(probe) in _VALID_PARSE_SHAPES
    )


def recognize_sql_message(text: str) -> SqlMessage | None:
    """按设计 §5.1 识别纯 SQL 消息；不是 SQL 时返回 ``None``（按普通对话处理）。

    返回的 ``SqlMessage`` 保存原文正文，不是判定副本。
    """
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

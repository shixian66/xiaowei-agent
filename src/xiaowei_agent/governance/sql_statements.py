"""SQL 语句识别登记表（设计 §5.1）：识别“是不是 SQL”的唯一真源。

每一行是一种顶层语句形式，写明两件事：

- **签名**：只有这种 SQL 才会有的开头（如 ``SHOW DATA``、``CREATE TABLE``、``PREPARE <名字>``）。
  签名在宽松切分的 token 上匹配，未闭合的字符串也能判断；命中签名说明“像 SQL”。
- **完整形状**：从签名起整条语句的 token 文法（复用只读清单的文法元素，完整匹配），或交
  sqlglot 判断“完整有效”（``SQLGLOT``，只用于 SELECT/WITH 这类文法开放、sqlglot 能严格解析的
  语句族），或一个只看 token 的判定函数。

登记范围是 StarRocks 文档 ``docs/en/sql-reference/sql-statements`` 下的全部顶层 SQL 语句（每行的
``docs`` 写明对应文档页；快照与完整性测试见 ``tests/unit/test_sql_statement_registry.py``），另加
MySQL 兼容的事务、预处理与常见写语句。登记只影响“是不是 SQL”，不授予任何权限：是否支持、
是否只读由 SQLGuard 判断。

以 SQL 关键字开头的文本按三档处理（负责人 2026-09-28 决定）：完整形状 → SQL；只命中签名 →
“像 SQL 但无法确认”，固定提示放入 sql 代码块重发，不进模型；签名都不命中 → 对话。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from xiaowei_agent.governance.readonly_statements import (
    AnyStr,
    GrammarElement,
    Int,
    Kw,
    Name,
    OneOf,
    Opt,
    Rest,
    Sym,
    matches_completely,
)
from xiaowei_agent.governance.sql_tokens import TokenKind, TokenScan

__all__ = [
    "SQLGLOT",
    "SQL_STATEMENT_FORMS",
    "SqlStatementForm",
    "lenient_tokens",
    "shape_matches",
    "signature_matches",
]


@dataclass(frozen=True, slots=True)
class LenientToken:
    """宽松切分的 token：``kind`` 为 word/number/string/quoted/symbol，``text`` 词为大写。"""

    kind: str
    text: str
    start: int


_LENIENT: Final = re.compile(
    r"""
    (?P<space>\s+|--[^\n]*|\#[^\n]*|/\*.*?(?:\*/|\Z))
    |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    |(?P<number>[0-9]+(?:\.[0-9]+)?)
    |(?P<quoted>`(?:[^`]|``)*`?)
    |(?P<string>'(?:[^'\\]|\\.|'')*'?|"(?:[^"\\]|\\.|"")*"?)
    |(?P<symbol>\S)
    """,
    re.VERBOSE | re.DOTALL,
)


def lenient_tokens(text: str) -> tuple[LenientToken, ...]:
    """跳过空白与注释（含 hint）的宽松切分；未闭合的字符串吃到末尾，不抛错。"""
    tokens: list[LenientToken] = []
    for match in _LENIENT.finditer(text):
        kind = match.lastgroup
        if kind is None or kind == "space":
            continue
        value = match.group()
        tokens.append(
            LenientToken(kind=kind, text=value.upper() if kind == "word" else value,
                         start=match.start())
        )
    return tuple(tokens)


# --- 签名 -----------------------------------------------------------------------

_Signature = tuple[str, ...] | Callable[[tuple[LenientToken, ...]], bool]
"""字符串元组逐个匹配 token：``A|B`` 为关键字之一，``<name>`` 为词或反引号标识符，``<str>``、
``<num>`` 为字面量，其余是符号。函数签名用于开头无法用固定前缀描述的语句族。"""


def _slot_matches(slot: str, token: LenientToken) -> bool:
    if "|" in slot and not slot[0].isalpha():
        return any(_slot_matches(option, token) for option in slot.split("|"))
    if slot == "<name>":
        return token.kind in {"word", "quoted"}
    if slot == "<str>":
        return token.kind == "string"
    if slot == "<num>":
        return token.kind == "number"
    if slot[0].isalpha():
        return token.kind == "word" and token.text in slot.split("|")
    return token.kind == "symbol" and token.text == slot


def signature_matches(signature: _Signature, tokens: tuple[LenientToken, ...]) -> bool:
    if callable(signature):
        return signature(tokens)
    return len(tokens) >= len(signature) and all(
        _slot_matches(slot, token) for slot, token in zip(signature, tokens, strict=False)
    )


def _has_word_after(first: int, *words: str) -> Callable[[tuple[LenientToken, ...]], bool]:
    def check(tokens: tuple[LenientToken, ...]) -> bool:
        return any(t.kind == "word" and t.text in words for t in tokens[first:])

    return check


_SELECT_LEADS: Final = frozenset({"*", "(", "@"})


def _alone(keyword: str, *tails: str) -> Callable[[tuple[LenientToken, ...]], bool]:
    """整条只是这个关键字（可带 ``tails`` 里的一个词与结尾分号）：单词语句的签名。"""

    def check(tokens: tuple[LenientToken, ...]) -> bool:
        texts = [token.text for token in tokens]
        if texts[-1:] == [";"]:
            texts = texts[:-1]
        return texts[:1] == [keyword] and (
            len(texts) == 1 or (len(texts) == 2 and texts[1] in tails)
        )

    return check


def _select_signature(tokens: tuple[LenientToken, ...]) -> bool:
    """SELECT 之后是表达式的明显开头，或后面出现 FROM。"""
    if len(tokens) < 2 or tokens[0].text != "SELECT":
        return False
    second = tokens[1]
    if second.kind in {"number", "string", "quoted"} or second.text in _SELECT_LEADS:
        return True
    if second.kind == "word" and second.text in {"DISTINCT", "ALL"}:
        return True
    if len(tokens) >= 3 and second.kind == "word" and tokens[2].text in {",", ".", "("}:
        return True
    return _has_word_after(2, "FROM")(tokens)


def _with_signature(tokens: tuple[LenientToken, ...]) -> bool:
    """``WITH [RECURSIVE] 名字 [(列…)] AS (``：公共表表达式。"""
    words = [t.text for t in tokens[:64]]
    return any(
        words[i] == "AS" and words[i + 1] == "(" for i in range(2, len(words) - 1)
    )


# --- 完整形状 ---------------------------------------------------------------------


class _Sqlglot:
    """交 sqlglot：解析为语句或因嵌套过深放弃才算完整有效（``Command`` 不算）。"""


SQLGLOT: Final = _Sqlglot()

_Shape = tuple[GrammarElement, ...] | _Sqlglot | Callable[[TokenScan], bool]


_PRIVILEGES: Final = frozenset(
    {
        "ALL", "SELECT", "INSERT", "UPDATE", "DELETE", "USAGE", "CREATE", "DROP", "ALTER",
        "OPERATE", "NODE", "GRANT", "IMPERSONATE", "REFRESH", "EXPORT", "REPOSITORY",
        "BLACKLIST", "FILE", "PLUGIN", "APPLY", "SECURITY",
    }
)
"""StarRocks 权限名；``GRANT/REVOKE <权限> …`` 的第二个词。"""


def _grant_signature(target: str) -> Callable[[tuple[LenientToken, ...]], bool]:
    """``GRANT <权限>`` 或 ``GRANT <角色> {, | TO}``（REVOKE 用 FROM）。"""

    def check(tokens: tuple[LenientToken, ...]) -> bool:
        if len(tokens) < 3:
            return False
        second, third = tokens[1], tokens[2]
        if second.kind == "word" and second.text in _PRIVILEGES:
            return True
        return second.kind in {"word", "quoted", "string"} and third.text in {",", target}

    return check


def _grant_shape(target: str) -> Callable[[TokenScan], bool]:
    """权限形式：权限词 … ON … TO/FROM …；角色形式：角色列表 TO/FROM [USER|ROLE] 用户。"""

    def check(scan: TokenScan) -> bool:
        texts = [scan.text(token).upper() for token in scan.statement_tokens]
        kinds = [token.kind for token in scan.statement_tokens]
        if texts[1] in _PRIVILEGES and "ON" in texts[2:]:
            on = texts.index("ON", 2)
            return target in texts[on + 1 :] and texts[-1] != target
        pos = 1
        while pos < len(texts):
            if kinds[pos] not in {TokenKind.WORD, TokenKind.QUOTED_IDENTIFIER, TokenKind.STRING}:
                return False
            pos += 1
            if pos < len(texts) and texts[pos] == ",":
                pos += 1
                continue
            break
        if pos >= len(texts) or texts[pos] != target:
            return False
        rest = texts[pos + 1 :]
        if rest[:1] in (["USER"], ["ROLE"]):
            rest = rest[1:]
        return 1 <= len(rest) <= 3
    return check


def shape_matches(shape: _Shape, scan: TokenScan) -> bool | None:
    """文法或判定函数的结论；``SQLGLOT`` 返回 ``None``，由调用方交 sqlglot 判断。"""
    if isinstance(shape, _Sqlglot):
        return None
    if isinstance(shape, tuple):
        return matches_completely(scan, shape)
    return shape(scan)


@dataclass(frozen=True, slots=True)
class SqlStatementForm:
    keyword: str
    signature: _Signature
    shape: _Shape
    docs: tuple[str, ...]
    """对应的 StarRocks 文档页名；MySQL 兼容语句写 ``mysql:<名字>``。"""


def _n(capture: str = "name") -> Name:
    return Name(capture, 1, 3)


_NAME: Final = _n()
_NAME_OR_STR: Final = OneOf((_n(),), (AnyStr(),))
_USER: Final = (OneOf((_n(),), (AnyStr(),)), Opt(Sym("@"), OneOf((_n("host"),), (AnyStr(),))))
_IF_EXISTS: Final = Opt(Kw("IF", "EXISTS"))
_IF_NOT_EXISTS: Final = Opt(Kw("IF", "NOT", "EXISTS"))
_FORCE: Final = Opt(Kw("FORCE"))
_REST: Final = Rest("rest")
_OPT_REST: Final = Opt(Rest("rest"))
_FROM_OR_IN: Final = OneOf((Kw("FROM"),), (Kw("IN"),))
_OPT_FROM_DB: Final = Opt(_FROM_OR_IN, _n("db"))
_OPT_LIKE: Final = Opt(Kw("LIKE"), AnyStr())
_OPT_FILTERS: Final = Opt(
    OneOf((Kw("WHERE"),), (Kw("ORDER", "BY"),), (Kw("LIMIT"),)), Rest("filters")
)


def _then(*keywords: str) -> OneOf:
    """后面必须是这些关键字（或符号）之一，再接任意剩余：写语句的结构锚点。"""
    return OneOf(*(((Sym(k),) if not k[0].isalpha() else (Kw(*k.split()),)) for k in keywords))


def _form(keyword: str, signature: _Signature, shape: _Shape, *docs: str) -> SqlStatementForm:
    return SqlStatementForm(keyword=keyword, signature=signature, shape=shape, docs=docs)


def _show(words: str, tail: tuple[GrammarElement, ...], *docs: str) -> SqlStatementForm:
    parts = words.split()
    return _form("SHOW", ("SHOW", *parts), (Kw("SHOW", *parts), *tail), *docs)


SQL_STATEMENT_FORMS: Final[tuple[SqlStatementForm, ...]] = (
    # ---- ADD / DELETE 黑名单 ----
    _form(
        "ADD", ("ADD", "BACKEND|COMPUTE"),
        (Kw("ADD"), OneOf((Kw("BACKEND"),), (Kw("COMPUTE", "NODE"),)), Kw("BLACKLIST"),
         Int(), _OPT_REST),
        "ADD_BACKEND_BLACKLIST",
    ),
    _form("ADD", ("ADD", "SQLBLACKLIST"), (Kw("ADD", "SQLBLACKLIST"), AnyStr()),
          "ADD_SQLBLACKLIST"),
    _form(
        "DELETE", ("DELETE", "BACKEND|COMPUTE"),
        (Kw("DELETE"), OneOf((Kw("BACKEND"),), (Kw("COMPUTE", "NODE"),)), Kw("BLACKLIST"),
         Int(), _OPT_REST),
        "DELETE_BACKEND_BLACKLIST",
    ),
    _form("DELETE", ("DELETE", "SQLBLACKLIST"), (Kw("DELETE", "SQLBLACKLIST"), Int(), _OPT_REST),
          "DELETE_SQLBLACKLIST"),
    _form("DELETE", ("DELETE", "FROM"),
          (Kw("DELETE", "FROM"), _NAME, Opt(_then("WHERE", "PARTITION", "USING"), _REST)),
          "DELETE"),
    # ---- ADMIN ----
    _form("ADMIN", ("ADMIN", "CANCEL", "REPAIR"),
          (Kw("ADMIN", "CANCEL", "REPAIR", "TABLE"), _NAME, Opt(Kw("PARTITION"), _REST)),
          "ADMIN_CANCEL_REPAIR"),
    _form("ADMIN", ("ADMIN", "CHECK", "TABLET"),
          (Kw("ADMIN", "CHECK", "TABLET"), Sym("("), _REST), "ADMIN_CHECK_TABLET"),
    _form("ADMIN", ("ADMIN", "REPAIR", "TABLE"),
          (Kw("ADMIN", "REPAIR", "TABLE"), _NAME, Opt(_then("PARTITION", "PROPERTIES"), _REST)),
          "ADMIN_REPAIR"),
    _form("ADMIN", ("ADMIN", "SET", "FRONTEND|TABLE|REPLICA"),
          (Kw("ADMIN", "SET"),
           OneOf((Kw("FRONTEND", "CONFIG"), Sym("(")), (Kw("TABLE"), _NAME, Kw("PARTITION")),
                 (Kw("REPLICA", "STATUS", "PROPERTIES"),)),
           _REST),
          "ADMIN_SET_CONFIG", "ADMIN_SET_PARTITION_VERSION", "ADMIN_SET_REPLICA_STATUS"),
    _form("ADMIN", ("ADMIN", "SHOW", "FRONTEND"),
          (Kw("ADMIN", "SHOW", "FRONTEND", "CONFIG"), _OPT_LIKE), "ADMIN_SHOW_CONFIG"),
    _form("ADMIN", ("ADMIN", "SHOW", "REPLICA|TABLET"),
          (Kw("ADMIN", "SHOW"),
           OneOf((Kw("REPLICA", "DISTRIBUTION"),), (Kw("REPLICA", "STATUS"),),
                 (Kw("TABLET", "STATUS"),)),
           Kw("FROM"), _NAME, Opt(_then("PARTITION", "WHERE", "PROPERTIES"), _REST)),
          "ADMIN_SHOW_REPLICA_DISTRIBUTION", "ADMIN_SHOW_REPLICA_STATUS",
          "ADMIN_SHOW_TABLET_STATUS"),
    _form("ADMIN", ("ADMIN", "SKIP", "COMMITTED"),
          (Kw("ADMIN", "SKIP", "COMMITTED", "TRANSACTION"), Int(), Opt(Kw("REASON"), AnyStr())),
          "ADMIN_SKIP_COMMITTED_TRANSACTION"),
    # ---- ALTER ----
    _form("ALTER", ("ALTER", "AI", "PROVIDER"),
          (Kw("ALTER", "AI", "PROVIDER"), _IF_EXISTS, _NAME, Kw("SET"), _REST),
          "ALTER_AI_PROVIDER"),
    _form("ALTER", ("ALTER", "DATABASE"),
          (Kw("ALTER", "DATABASE"), _NAME, _then("SET", "RENAME"), _REST), "ALTER_DATABASE"),
    _form("ALTER", ("ALTER", "LOAD", "FOR"),
          (Kw("ALTER", "LOAD", "FOR"), _NAME, Kw("PROPERTIES"), _REST), "ALTER_LOAD"),
    _form("ALTER", ("ALTER", "MATERIALIZED", "VIEW"),
          (Kw("ALTER", "MATERIALIZED", "VIEW"), _NAME,
           OneOf((Kw("ACTIVE"),), (Kw("INACTIVE"),),
                 (_then("RENAME", "REFRESH", "SWAP", "ORDER", "SET"), _REST))),
          "ALTER_MATERIALIZED_VIEW"),
    _form("ALTER", ("ALTER", "PIPE"),
          (Kw("ALTER", "PIPE"), _NAME,
           OneOf((Kw("SUSPEND"),), (Kw("RESUME"), _OPT_REST), (_then("SET", "RETRY"), _REST))),
          "ALTER_PIPE", "RETRY_FILE", "SUSPEND_or_RESUME_PIPE"),
    _form("ALTER", ("ALTER", "RESOURCE", "<name>|<str>"),
          (Kw("ALTER", "RESOURCE"), _NAME_OR_STR, Kw("SET", "PROPERTIES"), _REST),
          "ALTER_RESOURCE"),
    _form("ALTER", ("ALTER", "RESOURCE", "GROUP"),
          (Kw("ALTER", "RESOURCE", "GROUP"), _NAME, _then("ADD", "DROP", "WITH"), _REST),
          "ALTER_RESOURCE_GROUP"),
    _form("ALTER", ("ALTER", "ROUTINE", "LOAD"),
          (Kw("ALTER", "ROUTINE", "LOAD", "FOR"), _NAME, _REST), "ALTER_ROUTINE_LOAD"),
    _form("ALTER", ("ALTER", "STORAGE", "VOLUME"),
          (Kw("ALTER", "STORAGE", "VOLUME"), _IF_EXISTS, _NAME, _then("COMMENT", "SET"), _REST),
          "ALTER_STORAGE_VOLUME"),
    _form("ALTER", ("ALTER", "SYSTEM"),
          (Kw("ALTER", "SYSTEM"), _then("ADD", "DROP", "DECOMMISSION", "MODIFY", "SET"), _REST),
          "ALTER_SYSTEM"),
    _form("ALTER", ("ALTER", "TABLE"),
          (Kw("ALTER", "TABLE"), _NAME,
           _then("ADD", "DROP", "MODIFY", "RENAME", "SET", "REORDER", "COMMENT", "SWAP",
                 "COMPACT", "ORDER", "PARTITION", "DISTRIBUTED", "REPLACE", "TRUNCATE",
                 "OPTIMIZE", "BASE", "CUMULATIVE", "ENABLE", "DISABLE", "AUTO_INCREMENT"),
           _OPT_REST),
          "ALTER_TABLE"),
    _form("ALTER", ("ALTER", "TASK"),
          (Kw("ALTER", "TASK"), _IF_EXISTS, _NAME,
           OneOf((Kw("RESUME"),), (Kw("SUSPEND"),), (Kw("SET"), _REST))),
          "ALTER_TASK"),
    _form("ALTER", ("ALTER", "USER"),
          (Kw("ALTER", "USER"), _IF_EXISTS, *_USER, _then("IDENTIFIED", "DEFAULT", "SET"), _REST),
          "ALTER_USER"),
    _form("ALTER", ("ALTER", "VIEW"),
          (Kw("ALTER", "VIEW"), _NAME, _then("(", "AS", "SET", "MODIFY"), _REST), "ALTER_VIEW"),
    # ---- ANALYZE ----
    _form("ANALYZE", ("ANALYZE", "PROFILE"),
          (Kw("ANALYZE", "PROFILE", "FROM"), AnyStr(), Opt(Sym(","), _REST)), "ANALYZE_PROFILE"),
    _form("ANALYZE", ("ANALYZE", "TABLE|FULL|SAMPLE"),
          (Kw("ANALYZE"), Opt(OneOf((Kw("FULL"),), (Kw("SAMPLE"),))), Kw("TABLE"), _NAME,
           _OPT_REST),
          "ANALYZE_TABLE"),
    # ---- BACKUP / RESTORE ----
    _form("BACKUP", ("BACKUP", "SNAPSHOT|DATABASE|ALL"),
          (Kw("BACKUP"), _then("SNAPSHOT", "DATABASE", "ALL"), _NAME, Kw("TO"), _NAME,
           _OPT_REST),
          "BACKUP"),
    _form("RESTORE", ("RESTORE", "SNAPSHOT|DATABASE|ALL"),
          (Kw("RESTORE"), _then("SNAPSHOT", "DATABASE", "ALL"), _NAME, Kw("FROM"), _NAME,
           _OPT_REST),
          "RESTORE"),
    # ---- LOAD ----
    _form("LOAD", ("LOAD", "LABEL"), (Kw("LOAD", "LABEL"), _NAME, Sym("("), _REST),
          "BROKER_LOAD", "SPARK_LOAD"),
    _form("LOAD", ("LOAD", "DATA", "INFILE|LOCAL"),
          (Kw("LOAD", "DATA"), _then("INFILE", "LOCAL"), _REST),
          "mysql:LOAD_DATA"),
    # ---- CANCEL ----
    _form("CANCEL", ("CANCEL", "ALTER", "TABLE"),
          (Kw("CANCEL", "ALTER", "TABLE"),
           OneOf((Kw("COLUMN"),), (Kw("OPTIMIZE"),), (Kw("ROLLUP"),)),
           Kw("FROM"), _NAME, _OPT_REST),
          "CANCEL_ALTER_TABLE"),
    _form("CANCEL", ("CANCEL", "BACKUP|RESTORE"),
          (Kw("CANCEL"), OneOf((Kw("BACKUP"),), (Kw("RESTORE"),)),
           OneOf((Kw("FROM"), _NAME), (Kw("FOR", "EXTERNAL", "CATALOG"),))),
          "CANCEL_BACKUP", "CANCEL_RESTORE"),
    _form("CANCEL", ("CANCEL", "DECOMMISSION"),
          (Kw("CANCEL", "DECOMMISSION", "BACKEND"), AnyStr(), Opt(Sym(","), _REST)),
          "CANCEL_DECOMMISSION"),
    _form("CANCEL", ("CANCEL", "EXPORT|LOAD"),
          (Kw("CANCEL"), OneOf((Kw("EXPORT"),), (Kw("LOAD"),)), Opt(Kw("FROM"), _n("db")),
           Kw("WHERE"), _REST),
          "CANCEL_EXPORT", "CANCEL_LOAD"),
    _form("CANCEL", ("CANCEL", "REFRESH"),
          (Kw("CANCEL", "REFRESH"),
           OneOf((Kw("DICTIONARY"), _NAME), (Kw("MATERIALIZED", "VIEW"), _NAME, _FORCE))),
          "CANCEL_REFRESH_DICTIONARY", "CANCEL_REFRESH_MATERIALIZED_VIEW"),
    # ---- CREATE ----
    _form("CREATE", ("CREATE", "AI", "PROVIDER"),
          (Kw("CREATE", "AI", "PROVIDER"), _IF_NOT_EXISTS, _NAME, Kw("TYPE"), _REST),
          "CREATE_AI_PROVIDER"),
    _form("CREATE", ("CREATE", "ANALYZE"),
          (Kw("CREATE", "ANALYZE"), Opt(OneOf((Kw("FULL"),), (Kw("SAMPLE"),))),
           OneOf((Kw("ALL"),), (Kw("DATABASE"), _NAME), (Kw("TABLE"), _NAME)), _OPT_REST),
          "CREATE_ANALYZE"),
    _form("CREATE", ("CREATE", "DATABASE|SCHEMA"),
          (Kw("CREATE"), OneOf((Kw("DATABASE"),), (Kw("SCHEMA"),)), _IF_NOT_EXISTS, _NAME,
           Opt(Kw("PROPERTIES"), _REST)),
          "CREATE_DATABASE"),
    _form("CREATE", ("CREATE", "DICTIONARY"),
          (Kw("CREATE", "DICTIONARY"), _NAME, Kw("USING"), _REST), "CREATE_DICTIONARY"),
    _form("CREATE", ("CREATE", "EXTERNAL", "CATALOG"),
          (Kw("CREATE", "EXTERNAL", "CATALOG"), _IF_NOT_EXISTS, _NAME,
           _then("COMMENT", "PROPERTIES"), _REST),
          "CREATE_EXTERNAL_CATALOG"),
    _form("CREATE", ("CREATE", "FILE"), (Kw("CREATE", "FILE"), AnyStr(), _OPT_REST),
          "CREATE_FILE"),
    _form("CREATE", ("CREATE", "OR|GLOBAL|AGGREGATE|FUNCTION"),
          (Kw("CREATE"), Opt(Kw("OR", "REPLACE")), Opt(Kw("GLOBAL")),
           Opt(OneOf((Kw("AGGREGATE"),), (Kw("TABLE"),))), Kw("FUNCTION"), _NAME, Sym("("), _REST),
          "CREATE_FUNCTION"),
    _form("CREATE", ("CREATE", "INDEX"),
          (Kw("CREATE", "INDEX"), _NAME, Kw("ON"), _n("table"), Sym("("), _REST),
          "CREATE_INDEX"),
    _form("CREATE", ("CREATE", "MATERIALIZED", "VIEW"),
          (Kw("CREATE", "MATERIALIZED", "VIEW"), _IF_NOT_EXISTS, _NAME,
           _then("AS", "COMMENT", "PROPERTIES", "DISTRIBUTED", "REFRESH", "PARTITION", "ORDER"),
           _REST),
          "CREATE_MATERIALIZED_VIEW"),
    _form("CREATE", ("CREATE", "OR|PIPE"),
          (Kw("CREATE"), Opt(Kw("OR", "REPLACE")), Kw("PIPE"), _NAME, _then("PROPERTIES", "AS"),
           _REST),
          "CREATE_PIPE"),
    _form("CREATE", ("CREATE", "READ|REPOSITORY"),
          (Kw("CREATE"), Opt(Kw("READ", "ONLY")), Kw("REPOSITORY"), _NAME, Kw("WITH"), _REST),
          "CREATE_REPOSITORY"),
    _form("CREATE", ("CREATE", "EXTERNAL|RESOURCE"),
          (Kw("CREATE"), Opt(Kw("EXTERNAL")), Kw("RESOURCE"), _NAME_OR_STR, Kw("PROPERTIES"),
           _REST),
          "CREATE_RESOURCE"),
    _form("CREATE", ("CREATE", "RESOURCE", "GROUP"),
          (Kw("CREATE", "RESOURCE", "GROUP"), _IF_NOT_EXISTS, _NAME, _then("TO", "WITH"), _REST),
          "CREATE_RESOURCE_GROUP"),
    _form("CREATE", ("CREATE", "ROLE"), (Kw("CREATE", "ROLE"), _IF_NOT_EXISTS, _NAME, _OPT_REST),
          "CREATE_ROLE"),
    _form("CREATE", ("CREATE", "ROUTINE", "LOAD"),
          (Kw("CREATE", "ROUTINE", "LOAD"), _NAME, Kw("ON"), _n("table"), _REST),
          "CREATE_ROUTINE_LOAD"),
    _form("CREATE", ("CREATE", "STORAGE", "VOLUME"),
          (Kw("CREATE", "STORAGE", "VOLUME"), _IF_NOT_EXISTS, _NAME, Kw("TYPE"), _REST),
          "CREATE_STORAGE_VOLUME"),
    _form("CREATE", ("CREATE", "TABLE|EXTERNAL|TEMPORARY"),
          (Kw("CREATE"), Opt(Kw("EXTERNAL")), Opt(Kw("TEMPORARY")), Kw("TABLE"), _IF_NOT_EXISTS,
           _NAME,
           _then("(", "AS", "LIKE", "ENGINE", "PRIMARY", "DUPLICATE", "AGGREGATE", "UNIQUE",
                 "DISTRIBUTED", "PARTITION", "ORDER", "PROPERTIES", "COMMENT"),
           _REST),
          "CREATE_TABLE", "CREATE_TABLE_AS_SELECT", "CREATE_TABLE_LIKE"),
    _form("CREATE", ("CREATE", "USER"),
          (Kw("CREATE", "USER"), _IF_NOT_EXISTS, *_USER,
           Opt(_then("IDENTIFIED", "DEFAULT", "PROPERTIES"), _REST)),
          "CREATE_USER"),
    _form("CREATE", ("CREATE", "OR|VIEW"),
          (Kw("CREATE"), Opt(Kw("OR", "REPLACE")), Kw("VIEW"), _IF_NOT_EXISTS, _NAME,
           _then("(", "AS", "COMMENT", "SECURITY"), _REST),
          "CREATE_VIEW"),
    # ---- DESC / DESCRIBE ----
    *(
        _form(keyword, (keyword, "<name>"),
              (Kw(keyword),
               OneOf((_NAME, Opt(Kw("ALL"))), (Kw("FILES"), Sym("("), _REST),
                     (Kw("AI", "PROVIDER"), _NAME), (Kw("STORAGE", "VOLUME"), _NAME))),
              "DESCRIBE", "DESC_AI_PROVIDER", "DESC_STORAGE_VOLUME")
        for keyword in ("DESC", "DESCRIBE")
    ),
    # ---- DROP ----
    _form("DROP", ("DROP", "AI", "PROVIDER"),
          (Kw("DROP", "AI", "PROVIDER"), _IF_EXISTS, _NAME), "DROP_AI_PROVIDER"),
    _form("DROP", ("DROP", "ANALYZE"), (Kw("DROP", "ANALYZE"), Int()), "DROP_ANALYZE"),
    _form("DROP", ("DROP", "CATALOG"), (Kw("DROP", "CATALOG"), _IF_EXISTS, _NAME),
          "DROP_CATALOG"),
    _form("DROP", ("DROP", "DATABASE|SCHEMA"),
          (Kw("DROP"), OneOf((Kw("DATABASE"),), (Kw("SCHEMA"),)), _IF_EXISTS, _NAME, _FORCE),
          "DROP_DATABASE"),
    _form("DROP", ("DROP", "DICTIONARY"), (Kw("DROP", "DICTIONARY"), _NAME, Opt(Kw("CACHE"))),
          "DROP_DICTIONARY"),
    _form("DROP", ("DROP", "FILE"), (Kw("DROP", "FILE"), AnyStr(), _OPT_REST), "DROP_FILE"),
    _form("DROP", ("DROP", "GLOBAL|FUNCTION"),
          (Kw("DROP"), Opt(Kw("GLOBAL")), Kw("FUNCTION"), _IF_EXISTS, _NAME, Sym("("), _REST),
          "DROP_FUNCTION"),
    _form("DROP", ("DROP", "INDEX"), (Kw("DROP", "INDEX"), _NAME, Kw("ON"), _n("table")),
          "DROP_INDEX"),
    _form("DROP", ("DROP", "MATERIALIZED", "VIEW"),
          (Kw("DROP", "MATERIALIZED", "VIEW"), _IF_EXISTS, _NAME, _FORCE),
          "DROP_MATERIALIZED_VIEW"),
    _form("DROP", ("DROP", "PIPE"), (Kw("DROP", "PIPE"), _IF_EXISTS, _NAME), "DROP_PIPE"),
    _form("DROP", ("DROP", "PREPARE"), (Kw("DROP", "PREPARE"), _NAME),
          "prepared_statement"),
    _form("DROP", ("DROP", "REPOSITORY"), (Kw("DROP", "REPOSITORY"), _NAME), "DROP_REPOSITORY"),
    _form("DROP", ("DROP", "RESOURCE", "<name>|<str>"),
          (Kw("DROP", "RESOURCE"), _NAME_OR_STR), "DROP_RESOURCE"),
    _form("DROP", ("DROP", "RESOURCE", "GROUP"), (Kw("DROP", "RESOURCE", "GROUP"), _NAME),
          "DROP_RESOURCE_GROUP"),
    _form("DROP", ("DROP", "ROLE"), (Kw("DROP", "ROLE"), _IF_EXISTS, _NAME), "DROP_ROLE"),
    _form("DROP", ("DROP", "SNAPSHOT"),
          (Kw("DROP", "SNAPSHOT"), Opt(_n("snapshot")), Kw("ON"), _NAME, _OPT_REST),
          "DROP_SNAPSHOT"),
    _form("DROP", ("DROP", "STATS"), (Kw("DROP", "STATS"), _NAME), "DROP_STATS"),
    _form("DROP", ("DROP", "STORAGE", "VOLUME"),
          (Kw("DROP", "STORAGE", "VOLUME"), _IF_EXISTS, _NAME), "DROP_STORAGE_VOLUME"),
    _form("DROP", ("DROP", "TABLE|TEMPORARY"),
          (Kw("DROP"), Opt(Kw("TEMPORARY")), Kw("TABLE"), _IF_EXISTS, _NAME, _FORCE),
          "DROP_TABLE"),
    _form("DROP", ("DROP", "TASK"), (Kw("DROP", "TASK"), _IF_EXISTS, _NAME, _FORCE),
          "DROP_TASK"),
    _form("DROP", ("DROP", "USER"), (Kw("DROP", "USER"), _IF_EXISTS, *_USER), "DROP_USER"),
    _form("DROP", ("DROP", "VIEW"), (Kw("DROP", "VIEW"), _IF_EXISTS, _NAME), "DROP_VIEW"),
    # ---- 预处理语句（StarRocks 3.2+）与 EXECUTE AS ----
    _form("PREPARE", ("PREPARE", "<name>"), (Kw("PREPARE"), _NAME, Kw("FROM"), AnyStr()),
          "prepared_statement"),
    _form("EXECUTE", ("EXECUTE", "<name>"),
          (Kw("EXECUTE"), _NAME, Opt(Kw("USING"), _REST)), "prepared_statement"),
    _form("EXECUTE", ("EXECUTE", "AS"),
          (Kw("EXECUTE", "AS"), *_USER, Kw("WITH", "NO", "REVERT")), "EXECUTE_AS"),
    _form("DEALLOCATE", ("DEALLOCATE", "PREPARE"), (Kw("DEALLOCATE", "PREPARE"), _NAME),
          "prepared_statement"),
    # ---- EXPLAIN / EXPORT / TRANSLATE ----
    _form("EXPLAIN",
          ("EXPLAIN", "LOGICAL|VERBOSE|COSTS|ANALYZE|SELECT|WITH|INSERT|UPDATE|DELETE"),
          (Kw("EXPLAIN"),
           Opt(OneOf((Kw("LOGICAL"),), (Kw("VERBOSE"),), (Kw("COSTS"),), (Kw("ANALYZE"),))),
           _then("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "("), _REST),
          "EXPLAIN", "EXPLAIN_ANALYZE"),
    _form("EXPORT", ("EXPORT", "TABLE"),
          (Kw("EXPORT", "TABLE"), _NAME, _then("TO", "PARTITION", "("), _REST), "EXPORT"),
    _form("TRANSLATE", ("TRANSLATE", "TRINO"), (Kw("TRANSLATE", "TRINO"), _REST),
          "TRANSLATE_TRINO"),
    # ---- 权限 ----
    _form("GRANT", _grant_signature("TO"), _grant_shape("TO"), "GRANT"),
    _form("REVOKE", _grant_signature("FROM"), _grant_shape("FROM"), "REVOKE"),
    # ---- INSERT / UPDATE / MySQL 写语句 ----
    _form("INSERT", ("INSERT", "INTO|OVERWRITE"),
          (Kw("INSERT"), OneOf((Kw("INTO"),), (Kw("OVERWRITE"),)),
           OneOf((Kw("FILES"), Sym("(")), (_NAME,)),
           _then("VALUES", "VALUE", "SELECT", "WITH", "(", "PARTITION", "TEMPORARY", "BY",
                 "PROPERTIES", "SET"),
           _REST),
          "INSERT"),
    _form("UPDATE", _has_word_after(2, "SET"), SQLGLOT, "UPDATE"),
    _form("REPLACE", ("REPLACE", "INTO"),
          (Kw("REPLACE", "INTO"), _NAME, _then("VALUES", "VALUE", "SELECT", "(", "SET"), _REST),
          "mysql:REPLACE"),
    _form("UPSERT", ("UPSERT", "INTO"),
          (Kw("UPSERT", "INTO"), _NAME, _then("VALUES", "SELECT", "("), _REST), "mysql:UPSERT"),
    _form("MERGE", ("MERGE", "INTO"), (Kw("MERGE", "INTO"), _NAME, _REST), "mysql:MERGE"),
    _form("RENAME", ("RENAME", "TABLE|USER"),
          (Kw("RENAME"), OneOf((Kw("TABLE"),), (Kw("USER"),)), _NAME, Kw("TO"), _NAME,
           _OPT_REST),
          "mysql:RENAME"),
    _form("CALL", ("CALL", "<name>", "("), (Kw("CALL"), _NAME, Sym("("), _REST), "mysql:CALL"),
    _form("TRUNCATE", ("TRUNCATE", "TABLE"),
          (Kw("TRUNCATE", "TABLE"), _NAME, Opt(Kw("PARTITION"), _REST)), "TRUNCATE_TABLE"),
    _form("LOCK", ("LOCK", "TABLES|TABLE"),
          (Kw("LOCK"), OneOf((Kw("TABLES"),), (Kw("TABLE"),)), _NAME, _REST), "mysql:LOCK"),
    _form("UNLOCK", _alone("UNLOCK", "TABLES"), (Kw("UNLOCK", "TABLES"),), "mysql:UNLOCK"),
    # ---- 事务 ----
    *(
        _form(keyword, _alone(keyword, "WORK"), (Kw(keyword), Opt(Kw("WORK"))),
              "mysql:TRANSACTION")
        for keyword in ("BEGIN", "COMMIT", "ROLLBACK")
    ),
    _form("START", ("START", "TRANSACTION"), (Kw("START", "TRANSACTION"), _OPT_REST),
          "mysql:TRANSACTION"),
    # ---- 插件 / KILL / 例行导入 / 恢复 / 刷新 ----
    _form("INSTALL", ("INSTALL", "PLUGIN"),
          (Kw("INSTALL", "PLUGIN"), _IF_NOT_EXISTS, Kw("FROM"), _REST), "INSTALL_PLUGIN"),
    _form("UNINSTALL", ("UNINSTALL", "PLUGIN"),
          (Kw("UNINSTALL", "PLUGIN"), _IF_EXISTS, _NAME), "UNINSTALL_PLUGIN"),
    _form("KILL", ("KILL", "CONNECTION|QUERY"),
          (Kw("KILL"), OneOf((Kw("CONNECTION"),), (Kw("QUERY"),)), OneOf((Int(),), (AnyStr(),))),
          "KILL"),
    _form("KILL", ("KILL", "<num>"), (Kw("KILL"), Int()), "KILL"),
    _form("KILL", ("KILL", "<str>"), (Kw("KILL"), AnyStr()), "KILL"),
    _form("KILL", ("KILL", "ANALYZE"), (Kw("KILL", "ANALYZE"), Int()), "KILL_ANALYZE"),
    *(
        _form(keyword, (keyword, "ROUTINE", "LOAD"),
              (Kw(keyword, "ROUTINE", "LOAD", "FOR"), _NAME), doc)
        for keyword, doc in (
            ("PAUSE", "PAUSE_ROUTINE_LOAD"),
            ("RESUME", "RESUME_ROUTINE_LOAD"),
            ("STOP", "STOP_ROUTINE_LOAD"),
        )
    ),
    _form("RECOVER", ("RECOVER", "DATABASE|TABLE|PARTITION"),
          (Kw("RECOVER"),
           OneOf((Kw("DATABASE"), _NAME), (Kw("TABLE"), _NAME),
                 (Kw("PARTITION"), _n("partition"), Kw("FROM"), _NAME)),
           _OPT_REST),
          "RECOVER"),
    _form("REFRESH", ("REFRESH", "CONNECTIONS"), (Kw("REFRESH", "CONNECTIONS"), _FORCE),
          "REFRESH_CONNECTIONS"),
    _form("REFRESH", ("REFRESH", "DICTIONARY"), (Kw("REFRESH", "DICTIONARY"), _NAME),
          "REFRESH_DICTIONARY"),
    _form("REFRESH", ("REFRESH", "EXTERNAL", "TABLE"),
          (Kw("REFRESH", "EXTERNAL", "TABLE"), _NAME, Opt(Kw("PARTITION"), _REST)),
          "REFRESH_EXTERNAL_TABLE"),
    _form("REFRESH", ("REFRESH", "MATERIALIZED", "VIEW"),
          (Kw("REFRESH", "MATERIALIZED", "VIEW"), _NAME,
           Opt(_then("PARTITION", "FORCE", "WITH"), _OPT_REST)),
          "REFRESH_MATERIALIZED_VIEW"),
    # ---- SELECT / WITH ----
    _form("SELECT", _select_signature, SQLGLOT,
          "SELECT", "SELECT_DISTINCT", "SELECT_EXCEPT_MINUS", "SELECT_EXCLUDE",
          "SELECT_GROUP_BY", "SELECT_HAVING", "SELECT_INTERSECT", "SELECT_JOIN", "SELECT_LIMIT",
          "SELECT_OFFSET", "SELECT_ORDER_BY", "SELECT_PIVOT", "SELECT_UNION",
          "SELECT_WHERE_operator", "SELECT_alias", "SELECT_subquery"),
    _form("WITH", _with_signature, SQLGLOT, "SELECT_CTE"),
    # ---- SET ----
    _form("SET", ("SET", "CATALOG"), (Kw("SET", "CATALOG"), _NAME), "SET_CATALOG"),
    _form("SET", ("SET", "<name>", "AS", "DEFAULT"),
          (Kw("SET"), _NAME, Kw("AS", "DEFAULT"),
           OneOf((Kw("AI", "PROVIDER"),), (Kw("STORAGE", "VOLUME"),))),
          "SET_DEFAULT_AI_PROVIDER", "SET_DEFAULT_STORAGE_VOLUME"),
    _form("SET", ("SET", "DEFAULT", "ROLE"), (Kw("SET", "DEFAULT", "ROLE"), _REST),
          "SET_DEFAULT_ROLE"),
    _form("SET", ("SET", "PASSWORD"), (Kw("SET", "PASSWORD"), _then("=", "FOR"), _REST),
          "SET_PASSWORD"),
    _form("SET", ("SET", "ROLE"),
          (Kw("SET", "ROLE"),
           OneOf((Kw("ALL"),), (Kw("DEFAULT"),), (Kw("NONE"),), (AnyStr(),), (_NAME,)),
           Opt(_then(",", "EXCEPT"), _REST)),
          "SET_ROLE"),
    _form("SET", ("SET", "GLOBAL|SESSION"), (Kw("SET"), _then("GLOBAL", "SESSION"), _REST),
          "SET"),
    _form("SET", ("SET", "<name>", "="), (Kw("SET"), _NAME, Sym("="), _REST), "SET"),
    _form("SET", ("SET", "@"), (Kw("SET"), Sym("@"), _REST), "SET"),
    # ---- SHOW ----
    _show("AI PROVIDERS", (Opt(OneOf((Kw("LIKE"), AnyStr()), (Kw("TYPE"), _NAME))),),
          "SHOW_AI_PROVIDERS"),
    _show("ALTER", (_then("TABLE", "MATERIALIZED"), _OPT_REST),
          "SHOW_ALTER", "SHOW_ALTER_MATERIALIZED_VIEW"),
    _show("ANALYZE", (OneOf((Kw("JOB"),), (Kw("STATUS"),)), _OPT_FILTERS),
          "SHOW_ANALYZE_JOB", "SHOW_ANALYZE_STATUS"),
    _form("SHOW", ("SHOW", "ALL|AUTHENTICATION"),
          (Kw("SHOW"), Opt(Kw("ALL")), Kw("AUTHENTICATION"), Opt(Kw("FOR"), *_USER)),
          "SHOW_AUTHENTICATION"),
    _show("BACKENDS", (), "SHOW_BACKENDS"),
    _form("SHOW", ("SHOW", "BACKEND|COMPUTE"),
          (Kw("SHOW"), OneOf((Kw("BACKEND"),), (Kw("COMPUTE", "NODE"),)), Kw("BLACKLIST")),
          "SHOW_BACKEND_BLACKLIST"),
    _show("BACKUP", (_OPT_FROM_DB,), "SHOW_BACKUP"),
    _show("BROKER", (), "SHOW_BROKER"),
    _show("CATALOGS", (_OPT_LIKE,), "SHOW_CATALOGS"),
    _show("COMPUTE NODES", (), "SHOW_COMPUTE_NODES"),
    _show("CREATE",
          (OneOf((Kw("CATALOG"), _NAME), (Kw("DATABASE"), _NAME), (Kw("TABLE"), _NAME),
                 (Kw("VIEW"), _NAME), (Kw("MATERIALIZED", "VIEW"), _NAME),
                 (Kw("ROUTINE", "LOAD"), _NAME),
                 (Opt(Kw("GLOBAL")), Kw("FUNCTION"), _NAME, Sym("("), _REST)),),
          "SHOW_CREATE_CATALOG", "SHOW_CREATE_DATABASE", "SHOW_CREATE_FUNCTION",
          "SHOW_CREATE_MATERIALIZED_VIEW", "SHOW_CREATE_TABLE", "SHOW_CREATE_VIEW"),
    _show("DATA", (Opt(Kw("FROM"), _NAME), _OPT_FILTERS), "SHOW_DATA"),
    _show("DATABASES", (Opt(Kw("FROM"), _NAME), _OPT_LIKE), "SHOW_DATABASES"),
    _show("DELETE", (_OPT_FROM_DB,), "SHOW_DELETE"),
    _show("DICTIONARY", (Opt(_NAME),), "SHOW_DICTIONARY"),
    _show("DYNAMIC PARTITION TABLES", (_OPT_FROM_DB,), "SHOW_DYNAMIC_PARTITION_TABLES"),
    _show("EXPORT", (_OPT_FROM_DB, _OPT_FILTERS), "SHOW_EXPORT"),
    _show("FILE", (_OPT_FROM_DB,), "SHOW_FILE"),
    _show("FRONTENDS", (), "SHOW_FRONTENDS"),
    _form("SHOW", ("SHOW", "FULL|COLUMNS|FIELDS"),
          (Kw("SHOW"), Opt(Kw("FULL")), OneOf((Kw("COLUMNS"),), (Kw("FIELDS"),)), _FROM_OR_IN,
           _NAME, _OPT_FROM_DB, _OPT_LIKE),
          "SHOW_FULL_COLUMNS"),
    _form("SHOW", ("SHOW", "FULL|BUILTIN|FUNCTIONS"),
          (Kw("SHOW"), Opt(Kw("FULL")), Opt(Kw("BUILTIN")), Kw("FUNCTIONS"), _OPT_FROM_DB,
           _OPT_LIKE),
          "SHOW_FUNCTIONS"),
    _form("SHOW", ("SHOW", "FULL|PROCESSLIST"),
          (Kw("SHOW"), Opt(Kw("FULL")), Kw("PROCESSLIST")), "SHOW_PROCESSLIST"),
    _show("GRANTS", (Opt(Kw("FOR"), _REST),), "SHOW_GRANTS"),
    _form("SHOW", ("SHOW", "INDEX|INDEXES|KEY|KEYS"),
          (Kw("SHOW"), OneOf((Kw("INDEX"),), (Kw("INDEXES"),), (Kw("KEY"),), (Kw("KEYS"),)),
           _FROM_OR_IN, _NAME, _OPT_FROM_DB),
          "SHOW_INDEX"),
    _show("LOAD", (_OPT_FROM_DB, _OPT_FILTERS), "SHOW_LOAD"),
    _show("MATERIALIZED VIEWS", (_OPT_FROM_DB, _OPT_LIKE, _OPT_FILTERS),
          "SHOW_MATERIALIZED_VIEW"),
    _show("STATS META", (_OPT_FILTERS,), "SHOW_META"),
    _form("SHOW", ("SHOW", "TEMPORARY|PARTITIONS"),
          (Kw("SHOW"), Opt(Kw("TEMPORARY")), Kw("PARTITIONS", "FROM"), _NAME, _OPT_FILTERS),
          "SHOW_PARTITIONS"),
    _show("PIPES", (_OPT_FROM_DB, _OPT_FILTERS), "SHOW_PIPES"),
    _show("PLUGINS", (), "SHOW_PLUGINS"),
    _show("PROC", (AnyStr(),), "SHOW_PROC"),
    _show("PROFILELIST", (Opt(Kw("LIMIT"), Int()),), "SHOW_PROFILELIST"),
    _show("PROPERTY", (Opt(Kw("FOR"), AnyStr()), _OPT_LIKE), "SHOW_PROPERTY"),
    _show("REPOSITORIES", (), "SHOW_REPOSITORIES"),
    _show("RESOURCES", (_OPT_LIKE, _OPT_FILTERS), "SHOW_RESOURCES"),
    _show("RESOURCE", (OneOf((Kw("GROUPS"), Opt(Kw("ALL"))), (Kw("GROUP"), _NAME)),),
          "SHOW_RESOURCE_GROUP"),
    _show("RESTORE", (_OPT_FROM_DB, _OPT_FILTERS), "SHOW_RESTORE"),
    _show("ROLES", (), "SHOW_ROLES"),
    _form("SHOW", ("SHOW", "ALL|ROUTINE"),
          (Kw("SHOW"), Opt(Kw("ALL")), Kw("ROUTINE", "LOAD"),
           Opt(OneOf((Kw("FOR"), _NAME), (Kw("FROM"), _n("db")))), _OPT_FILTERS),
          "SHOW_ROUTINE_LOAD"),
    _show("ROUTINE LOAD TASK", (_OPT_FROM_DB, Kw("WHERE"), _REST), "SHOW_ROUTINE_LOAD_TASK"),
    _show("RUNNING QUERIES", (), "SHOW_RUNNING_QUERIES"),
    _show("SNAPSHOT", (Kw("ON"), _NAME, _OPT_FILTERS), "SHOW_SNAPSHOT"),
    _show("SQLBLACKLIST", (), "SHOW_SQLBLACKLIST"),
    _show("STORAGE VOLUMES", (_OPT_LIKE,), "SHOW_STORAGE_VOLUMES"),
    _form("SHOW", ("SHOW", "FULL|TABLES"),
          (Kw("SHOW"), Opt(Kw("FULL")), Kw("TABLES"), _OPT_FROM_DB, _OPT_LIKE),
          "SHOW_TABLES"),
    _show("TABLET", (OneOf((Int(),), (Kw("FROM"), _NAME, _OPT_REST)),), "SHOW_TABLET"),
    _show("TABLE STATUS", (_OPT_FROM_DB, _OPT_LIKE), "SHOW_TABLE_STATUS"),
    _show("TRANSACTION", (_OPT_FROM_DB, Kw("WHERE"), _REST), "SHOW_TRANSACTION"),
    _show("USAGE RESOURCE GROUPS", (), "SHOW_USAGE_RESOURCE_GROUPS"),
    _show("USERS", (), "SHOW_USERS"),
    _form("SHOW", ("SHOW", "GLOBAL|SESSION|VARIABLES"),
          (Kw("SHOW"), Opt(OneOf((Kw("GLOBAL"),), (Kw("SESSION"),))), Kw("VARIABLES"),
           Opt(OneOf((Kw("LIKE"), AnyStr()), (Kw("WHERE"), _REST)))),
          "SHOW_VARIABLES"),
    _form("SHOW", ("SHOW", "WARNINGS|ERRORS"),
          (Kw("SHOW"), OneOf((Kw("WARNINGS"),), (Kw("ERRORS"),)), Opt(Kw("LIMIT"), _REST)),
          "SHOW_WARNINGS"),
    # ---- 其他 ----
    _form("SUBMIT", ("SUBMIT", "TASK"),
          (Kw("SUBMIT", "TASK"), Opt(_NAME), _then("SCHEDULE", "PROPERTIES", "AS"), _REST),
          "SUBMIT_TASK"),
    _form("SYNC", _alone("SYNC"), (Kw("SYNC"),), "SYNC"),
    _form("USE", ("USE", "<name>"), (Kw("USE"), _NAME), "USE"),
)
"""登记表本身；关键字闭集与签名、形状都只从这里派生。"""

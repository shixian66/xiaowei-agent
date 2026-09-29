"""SQL 语句识别登记表（设计 §5.1）：识别“是不是 SQL”的唯一真源。

每一行是一种顶层语句形式，写明它的**签名**：只有这种 SQL 才会有的开头（如 ``SHOW DATA``、
``CREATE TABLE``、``PREPARE <名字>``）。签名在宽松切分的 token 上匹配，未闭合的字符串也能判断。
命中签名就是 SQL（负责人 2026-09-29 决定：像 SQL 的就是 SQL），写不完整、写错的照样保存为
SqlArtifact，由 SQLGuard 拒绝或交数据库执行后把报错返回给用户，不进模型。

登记范围是 StarRocks 文档 ``docs/en/sql-reference/sql-statements`` 下的全部顶层 SQL 语句（每行的
``docs`` 写明对应文档页；快照与完整性测试见 ``tests/unit/test_sql_statement_registry.py``），另加
MySQL 兼容的事务、预处理与常见写语句。登记只影响“是不是 SQL”，不授予任何权限：是否支持、
是否只读由 SQLGuard 判断。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "SQL_STATEMENT_FORMS",
    "SqlStatementForm",
    "lenient_tokens",
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
    """SELECT 之后是表达式的明显开头，或后面出现 FROM 或字符串（含未闭合的）。"""
    if len(tokens) < 2 or tokens[0].text != "SELECT":
        return False
    if any(token.kind == "string" for token in tokens[1:]):
        return True
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


@dataclass(frozen=True, slots=True)
class SqlStatementForm:
    keyword: str
    signature: _Signature
    docs: tuple[str, ...]
    """对应的 StarRocks 文档页名；MySQL 兼容语句写 ``mysql:<名字>``。"""


def _form(keyword: str, signature: _Signature, *docs: str) -> SqlStatementForm:
    return SqlStatementForm(keyword=keyword, signature=signature, docs=docs)


def _show(words: str, *docs: str) -> SqlStatementForm:
    return _form("SHOW", ("SHOW", *words.split()), *docs)


SQL_STATEMENT_FORMS: Final[tuple[SqlStatementForm, ...]] = (
    # ---- ADD / DELETE 黑名单 ----
    _form("ADD", ("ADD", "BACKEND|COMPUTE"), "ADD_BACKEND_BLACKLIST"),
    _form("ADD", ("ADD", "SQLBLACKLIST"), "ADD_SQLBLACKLIST"),
    _form("DELETE", ("DELETE", "BACKEND|COMPUTE"), "DELETE_BACKEND_BLACKLIST"),
    _form("DELETE", ("DELETE", "SQLBLACKLIST"), "DELETE_SQLBLACKLIST"),
    _form("DELETE", ("DELETE", "FROM"), "DELETE"),
    # ---- ADMIN ----
    _form("ADMIN", ("ADMIN", "CANCEL", "REPAIR"), "ADMIN_CANCEL_REPAIR"),
    _form("ADMIN", ("ADMIN", "CHECK", "TABLET"), "ADMIN_CHECK_TABLET"),
    _form("ADMIN", ("ADMIN", "REPAIR", "TABLE"), "ADMIN_REPAIR"),
    _form("ADMIN", ("ADMIN", "SET", "FRONTEND|TABLE|REPLICA"),
          "ADMIN_SET_CONFIG", "ADMIN_SET_PARTITION_VERSION", "ADMIN_SET_REPLICA_STATUS"),
    _form("ADMIN", ("ADMIN", "SHOW", "FRONTEND"), "ADMIN_SHOW_CONFIG"),
    _form("ADMIN", ("ADMIN", "SHOW", "REPLICA|TABLET"),
          "ADMIN_SHOW_REPLICA_DISTRIBUTION", "ADMIN_SHOW_REPLICA_STATUS",
          "ADMIN_SHOW_TABLET_STATUS"),
    _form("ADMIN", ("ADMIN", "SKIP", "COMMITTED"), "ADMIN_SKIP_COMMITTED_TRANSACTION"),
    # ---- ALTER ----
    _form("ALTER", ("ALTER", "AI", "PROVIDER"), "ALTER_AI_PROVIDER"),
    _form("ALTER", ("ALTER", "DATABASE"), "ALTER_DATABASE"),
    _form("ALTER", ("ALTER", "LOAD", "FOR"), "ALTER_LOAD"),
    _form("ALTER", ("ALTER", "MATERIALIZED", "VIEW"), "ALTER_MATERIALIZED_VIEW"),
    _form("ALTER", ("ALTER", "PIPE"), "ALTER_PIPE", "RETRY_FILE", "SUSPEND_or_RESUME_PIPE"),
    _form("ALTER", ("ALTER", "RESOURCE", "<name>|<str>"), "ALTER_RESOURCE"),
    _form("ALTER", ("ALTER", "RESOURCE", "GROUP"), "ALTER_RESOURCE_GROUP"),
    _form("ALTER", ("ALTER", "ROUTINE", "LOAD"), "ALTER_ROUTINE_LOAD"),
    _form("ALTER", ("ALTER", "STORAGE", "VOLUME"), "ALTER_STORAGE_VOLUME"),
    _form("ALTER", ("ALTER", "SYSTEM"), "ALTER_SYSTEM"),
    _form("ALTER", ("ALTER", "TABLE"), "ALTER_TABLE"),
    _form("ALTER", ("ALTER", "TASK"), "ALTER_TASK"),
    _form("ALTER", ("ALTER", "USER"), "ALTER_USER"),
    _form("ALTER", ("ALTER", "VIEW"), "ALTER_VIEW"),
    # ---- ANALYZE ----
    _form("ANALYZE", ("ANALYZE", "PROFILE"), "ANALYZE_PROFILE"),
    _form("ANALYZE", ("ANALYZE", "TABLE|FULL|SAMPLE"), "ANALYZE_TABLE"),
    # ---- BACKUP / RESTORE ----
    _form("BACKUP", ("BACKUP", "SNAPSHOT|DATABASE|ALL"), "BACKUP"),
    _form("RESTORE", ("RESTORE", "SNAPSHOT|DATABASE|ALL"), "RESTORE"),
    # ---- LOAD ----
    _form("LOAD", ("LOAD", "LABEL"), "BROKER_LOAD", "SPARK_LOAD"),
    _form("LOAD", ("LOAD", "DATA", "INFILE|LOCAL"), "mysql:LOAD_DATA"),
    # ---- CANCEL ----
    _form("CANCEL", ("CANCEL", "ALTER", "TABLE"), "CANCEL_ALTER_TABLE"),
    _form("CANCEL", ("CANCEL", "BACKUP|RESTORE"), "CANCEL_BACKUP", "CANCEL_RESTORE"),
    _form("CANCEL", ("CANCEL", "DECOMMISSION"), "CANCEL_DECOMMISSION"),
    _form("CANCEL", ("CANCEL", "EXPORT|LOAD"), "CANCEL_EXPORT", "CANCEL_LOAD"),
    _form("CANCEL", ("CANCEL", "REFRESH"),
          "CANCEL_REFRESH_DICTIONARY", "CANCEL_REFRESH_MATERIALIZED_VIEW"),
    # ---- CREATE ----
    _form("CREATE", ("CREATE", "AI", "PROVIDER"), "CREATE_AI_PROVIDER"),
    _form("CREATE", ("CREATE", "ANALYZE"), "CREATE_ANALYZE"),
    _form("CREATE", ("CREATE", "DATABASE|SCHEMA"), "CREATE_DATABASE"),
    _form("CREATE", ("CREATE", "DICTIONARY"), "CREATE_DICTIONARY"),
    _form("CREATE", ("CREATE", "EXTERNAL", "CATALOG"), "CREATE_EXTERNAL_CATALOG"),
    _form("CREATE", ("CREATE", "FILE"), "CREATE_FILE"),
    _form("CREATE", ("CREATE", "OR|GLOBAL|AGGREGATE|FUNCTION"), "CREATE_FUNCTION"),
    _form("CREATE", ("CREATE", "INDEX"), "CREATE_INDEX"),
    _form("CREATE", ("CREATE", "MATERIALIZED", "VIEW"), "CREATE_MATERIALIZED_VIEW"),
    _form("CREATE", ("CREATE", "OR|PIPE"), "CREATE_PIPE"),
    _form("CREATE", ("CREATE", "READ|REPOSITORY"), "CREATE_REPOSITORY"),
    _form("CREATE", ("CREATE", "EXTERNAL|RESOURCE"), "CREATE_RESOURCE"),
    _form("CREATE", ("CREATE", "RESOURCE", "GROUP"), "CREATE_RESOURCE_GROUP"),
    _form("CREATE", ("CREATE", "ROLE"), "CREATE_ROLE"),
    _form("CREATE", ("CREATE", "ROUTINE", "LOAD"), "CREATE_ROUTINE_LOAD"),
    _form("CREATE", ("CREATE", "STORAGE", "VOLUME"), "CREATE_STORAGE_VOLUME"),
    _form("CREATE", ("CREATE", "TABLE|EXTERNAL|TEMPORARY"),
          "CREATE_TABLE", "CREATE_TABLE_AS_SELECT", "CREATE_TABLE_LIKE"),
    _form("CREATE", ("CREATE", "USER"), "CREATE_USER"),
    _form("CREATE", ("CREATE", "OR|VIEW"), "CREATE_VIEW"),
    # ---- DESC / DESCRIBE ----
    *(
        _form(keyword, (keyword, "<name>"), "DESCRIBE", "DESC_AI_PROVIDER", "DESC_STORAGE_VOLUME")
        for keyword in ("DESC", "DESCRIBE")
    ),
    # ---- DROP ----
    _form("DROP", ("DROP", "AI", "PROVIDER"), "DROP_AI_PROVIDER"),
    _form("DROP", ("DROP", "ANALYZE"), "DROP_ANALYZE"),
    _form("DROP", ("DROP", "CATALOG"), "DROP_CATALOG"),
    _form("DROP", ("DROP", "DATABASE|SCHEMA"), "DROP_DATABASE"),
    _form("DROP", ("DROP", "DICTIONARY"), "DROP_DICTIONARY"),
    _form("DROP", ("DROP", "FILE"), "DROP_FILE"),
    _form("DROP", ("DROP", "GLOBAL|FUNCTION"), "DROP_FUNCTION"),
    _form("DROP", ("DROP", "INDEX"), "DROP_INDEX"),
    _form("DROP", ("DROP", "MATERIALIZED", "VIEW"), "DROP_MATERIALIZED_VIEW"),
    _form("DROP", ("DROP", "PIPE"), "DROP_PIPE"),
    _form("DROP", ("DROP", "PREPARE"), "prepared_statement"),
    _form("DROP", ("DROP", "REPOSITORY"), "DROP_REPOSITORY"),
    _form("DROP", ("DROP", "RESOURCE", "<name>|<str>"), "DROP_RESOURCE"),
    _form("DROP", ("DROP", "RESOURCE", "GROUP"), "DROP_RESOURCE_GROUP"),
    _form("DROP", ("DROP", "ROLE"), "DROP_ROLE"),
    _form("DROP", ("DROP", "SNAPSHOT"), "DROP_SNAPSHOT"),
    _form("DROP", ("DROP", "STATS"), "DROP_STATS"),
    _form("DROP", ("DROP", "STORAGE", "VOLUME"), "DROP_STORAGE_VOLUME"),
    _form("DROP", ("DROP", "TABLE|TEMPORARY"), "DROP_TABLE"),
    _form("DROP", ("DROP", "TASK"), "DROP_TASK"),
    _form("DROP", ("DROP", "USER"), "DROP_USER"),
    _form("DROP", ("DROP", "VIEW"), "DROP_VIEW"),
    # ---- 预处理语句（StarRocks 3.2+）与 EXECUTE AS ----
    _form("PREPARE", ("PREPARE", "<name>"), "prepared_statement"),
    _form("EXECUTE", ("EXECUTE", "<name>"), "prepared_statement"),
    _form("EXECUTE", ("EXECUTE", "AS"), "EXECUTE_AS"),
    _form("DEALLOCATE", ("DEALLOCATE", "PREPARE"), "prepared_statement"),
    # ---- EXPLAIN / EXPORT / TRANSLATE ----
    _form("EXPLAIN", ("EXPLAIN", "LOGICAL|VERBOSE|COSTS|ANALYZE|SELECT|WITH|INSERT|UPDATE|DELETE"),
          "EXPLAIN", "EXPLAIN_ANALYZE"),
    _form("EXPORT", ("EXPORT", "TABLE"), "EXPORT"),
    _form("TRANSLATE", ("TRANSLATE", "TRINO"), "TRANSLATE_TRINO"),
    # ---- 权限 ----
    _form("GRANT", _grant_signature("TO"), "GRANT"),
    _form("REVOKE", _grant_signature("FROM"), "REVOKE"),
    # ---- INSERT / UPDATE / MySQL 写语句 ----
    _form("INSERT", ("INSERT", "INTO|OVERWRITE"), "INSERT"),
    _form("UPDATE", _has_word_after(2, "SET"), "UPDATE"),
    _form("REPLACE", ("REPLACE", "INTO"), "mysql:REPLACE"),
    _form("UPSERT", ("UPSERT", "INTO"), "mysql:UPSERT"),
    _form("MERGE", ("MERGE", "INTO"), "mysql:MERGE"),
    _form("RENAME", ("RENAME", "TABLE|USER"), "mysql:RENAME"),
    _form("CALL", ("CALL", "<name>", "("), "mysql:CALL"),
    _form("TRUNCATE", ("TRUNCATE", "TABLE"), "TRUNCATE_TABLE"),
    _form("LOCK", ("LOCK", "TABLES|TABLE"), "mysql:LOCK"),
    _form("UNLOCK", _alone("UNLOCK", "TABLES"), "mysql:UNLOCK"),
    # ---- 事务 ----
    *(
        _form(keyword, _alone(keyword, "WORK"), "mysql:TRANSACTION")
        for keyword in ("BEGIN", "COMMIT", "ROLLBACK")
    ),
    _form("START", ("START", "TRANSACTION"), "mysql:TRANSACTION"),
    # ---- 插件 / KILL / 例行导入 / 恢复 / 刷新 ----
    _form("INSTALL", ("INSTALL", "PLUGIN"), "INSTALL_PLUGIN"),
    _form("UNINSTALL", ("UNINSTALL", "PLUGIN"), "UNINSTALL_PLUGIN"),
    _form("KILL", ("KILL", "CONNECTION|QUERY"), "KILL"),
    _form("KILL", ("KILL", "<num>"), "KILL"),
    _form("KILL", ("KILL", "<str>"), "KILL"),
    _form("KILL", ("KILL", "ANALYZE"), "KILL_ANALYZE"),
    *(
        _form(keyword, (keyword, "ROUTINE", "LOAD"), doc)
        for keyword, doc in (
            ("PAUSE", "PAUSE_ROUTINE_LOAD"),
            ("RESUME", "RESUME_ROUTINE_LOAD"),
            ("STOP", "STOP_ROUTINE_LOAD"),
        )
    ),
    _form("RECOVER", ("RECOVER", "DATABASE|TABLE|PARTITION"), "RECOVER"),
    _form("REFRESH", ("REFRESH", "CONNECTIONS"), "REFRESH_CONNECTIONS"),
    _form("REFRESH", ("REFRESH", "DICTIONARY"), "REFRESH_DICTIONARY"),
    _form("REFRESH", ("REFRESH", "EXTERNAL", "TABLE"), "REFRESH_EXTERNAL_TABLE"),
    _form("REFRESH", ("REFRESH", "MATERIALIZED", "VIEW"), "REFRESH_MATERIALIZED_VIEW"),
    # ---- SELECT / WITH ----
    _form("SELECT", _select_signature,
          "SELECT", "SELECT_DISTINCT", "SELECT_EXCEPT_MINUS", "SELECT_EXCLUDE", "SELECT_GROUP_BY",
          "SELECT_HAVING", "SELECT_INTERSECT", "SELECT_JOIN", "SELECT_LIMIT", "SELECT_OFFSET",
          "SELECT_ORDER_BY", "SELECT_PIVOT", "SELECT_UNION", "SELECT_WHERE_operator",
          "SELECT_alias", "SELECT_subquery"),
    _form("WITH", _with_signature, "SELECT_CTE"),
    # ---- SET ----
    _form("SET", ("SET", "CATALOG"), "SET_CATALOG"),
    _form("SET", ("SET", "<name>", "AS", "DEFAULT"),
          "SET_DEFAULT_AI_PROVIDER", "SET_DEFAULT_STORAGE_VOLUME"),
    _form("SET", ("SET", "DEFAULT", "ROLE"), "SET_DEFAULT_ROLE"),
    _form("SET", ("SET", "PASSWORD"), "SET_PASSWORD"),
    _form("SET", ("SET", "ROLE"), "SET_ROLE"),
    _form("SET", ("SET", "GLOBAL|SESSION"), "SET"),
    _form("SET", ("SET", "<name>", "="), "SET"),
    _form("SET", ("SET", "@"), "SET"),
    # ---- SHOW ----
    _show("AI PROVIDERS", "SHOW_AI_PROVIDERS"),
    _show("ALTER", "SHOW_ALTER", "SHOW_ALTER_MATERIALIZED_VIEW"),
    _show("ANALYZE", "SHOW_ANALYZE_JOB", "SHOW_ANALYZE_STATUS"),
    _form("SHOW", ("SHOW", "ALL|AUTHENTICATION"), "SHOW_AUTHENTICATION"),
    _show("BACKENDS", "SHOW_BACKENDS"),
    _form("SHOW", ("SHOW", "BACKEND|COMPUTE"), "SHOW_BACKEND_BLACKLIST"),
    _show("BACKUP", "SHOW_BACKUP"),
    _show("BROKER", "SHOW_BROKER"),
    _show("CATALOGS", "SHOW_CATALOGS"),
    _show("COMPUTE NODES", "SHOW_COMPUTE_NODES"),
    _show("CREATE",
          "SHOW_CREATE_CATALOG", "SHOW_CREATE_DATABASE", "SHOW_CREATE_FUNCTION",
          "SHOW_CREATE_MATERIALIZED_VIEW", "SHOW_CREATE_TABLE", "SHOW_CREATE_VIEW"),
    _show("DATA", "SHOW_DATA"),
    _show("DATABASES", "SHOW_DATABASES"),
    _show("DELETE", "SHOW_DELETE"),
    _show("DICTIONARY", "SHOW_DICTIONARY"),
    _show("DYNAMIC PARTITION TABLES", "SHOW_DYNAMIC_PARTITION_TABLES"),
    _show("EXPORT", "SHOW_EXPORT"),
    _show("FILE", "SHOW_FILE"),
    _show("FRONTENDS", "SHOW_FRONTENDS"),
    _form("SHOW", ("SHOW", "FULL|COLUMNS|FIELDS"), "SHOW_FULL_COLUMNS"),
    _form("SHOW", ("SHOW", "FULL|BUILTIN|FUNCTIONS"), "SHOW_FUNCTIONS"),
    _form("SHOW", ("SHOW", "FULL|PROCESSLIST"), "SHOW_PROCESSLIST"),
    _show("GRANTS", "SHOW_GRANTS"),
    _form("SHOW", ("SHOW", "INDEX|INDEXES|KEY|KEYS"), "SHOW_INDEX"),
    _show("LOAD", "SHOW_LOAD"),
    _show("MATERIALIZED VIEWS", "SHOW_MATERIALIZED_VIEW"),
    _show("STATS META", "SHOW_META"),
    _form("SHOW", ("SHOW", "TEMPORARY|PARTITIONS"), "SHOW_PARTITIONS"),
    _show("PIPES", "SHOW_PIPES"),
    _show("PLUGINS", "SHOW_PLUGINS"),
    _show("PROC", "SHOW_PROC"),
    _show("PROFILELIST", "SHOW_PROFILELIST"),
    _show("PROPERTY", "SHOW_PROPERTY"),
    _show("REPOSITORIES", "SHOW_REPOSITORIES"),
    _show("RESOURCES", "SHOW_RESOURCES"),
    _show("RESOURCE", "SHOW_RESOURCE_GROUP"),
    _show("RESTORE", "SHOW_RESTORE"),
    _show("ROLES", "SHOW_ROLES"),
    _form("SHOW", ("SHOW", "ALL|ROUTINE"), "SHOW_ROUTINE_LOAD"),
    _show("ROUTINE LOAD TASK", "SHOW_ROUTINE_LOAD_TASK"),
    _show("RUNNING QUERIES", "SHOW_RUNNING_QUERIES"),
    _show("SNAPSHOT", "SHOW_SNAPSHOT"),
    _show("SQLBLACKLIST", "SHOW_SQLBLACKLIST"),
    _show("STORAGE VOLUMES", "SHOW_STORAGE_VOLUMES"),
    _form("SHOW", ("SHOW", "FULL|TABLES"), "SHOW_TABLES"),
    _show("TABLET", "SHOW_TABLET"),
    _show("TABLE STATUS", "SHOW_TABLE_STATUS"),
    _show("TRANSACTION", "SHOW_TRANSACTION"),
    _show("USAGE RESOURCE GROUPS", "SHOW_USAGE_RESOURCE_GROUPS"),
    _show("USERS", "SHOW_USERS"),
    _form("SHOW", ("SHOW", "GLOBAL|SESSION|VARIABLES"), "SHOW_VARIABLES"),
    _form("SHOW", ("SHOW", "WARNINGS|ERRORS"), "SHOW_WARNINGS"),
    # ---- 其他 ----
    _form("SUBMIT", ("SUBMIT", "TASK"), "SUBMIT_TASK"),
    _form("SYNC", _alone("SYNC"), "SYNC"),
    _form("USE", ("USE", "<name>"), "USE"),
)
"""登记表本身；关键字闭集与签名、形状都只从这里派生。"""

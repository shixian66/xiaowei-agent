"""SQL AST 校验：唯一的 SQL 准入内核，两个 profile（设计 §7.1）。

- ``template_locked``（:func:`verify_sql`）：慢查询模板 SQL，闭集白名单 + 重编译比对；
- ``confirmed_readonly``（:func:`verify_confirmed_readonly`）：F1 用户直接提交的 SQL，原文
  hash、单语句、只读证明、内部 Catalog 与黑名单；不重编译、不改写，也不返回任何 SQL 文本。

以下是 ``template_locked`` 的设计说明。

安全性来自四段，任何一段单独存在都不足以支撑"已批准的计划不能执行另一条 SQL"：
参数闭集 → 确定性编译 → **AST 闭集校验** → **重编译逐字节比对**。本模块是后两段。

**规则顺序即拒绝原因的优先级。** 具体的 AST 规则先跑，重编译比对最后跑：

- 若比对排在最前，每一条攻击都会得到 ``RECOMPILE_MISMATCH``——审计里看到的会是
  最宽泛的原因，错误归因随之失真；
- 更糟的是，AST 规则会全部变成**不可达的死代码**：任何不等于我们输出的 SQL 都被
  第一条拦下，任何等于我们输出的 SQL 都必然通过其余规则。纵深防御于是只剩一层。

因此比对是最后一道闸，而不是第一道。

**用闭集而非黑名单**：``_ALLOWED_NODES`` 之外的节点类型一律拒绝，所以 ``OR 1=1``、
``UNION``、子查询、CTE、JOIN、表别名、``FOR UPDATE``、``:=``、``INTO OUTFILE``、
任意函数调用都由同一条规则挡下——新的绕过写法默认被拒。
"""

import datetime as _dt
import hashlib
import logging
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from xiaowei_agent.contracts import SqlGuardRejection, SqlSurface
from xiaowei_agent.contracts.sql_query import QualifiedRelation
from xiaowei_agent.governance.readonly_statements import StatementMatch, match_statement
from xiaowei_agent.governance.sql_tokens import (
    TokenScan,
    TokenScanError,
    TokenScanReason,
    scan_sql,
)
from xiaowei_agent.planning.starrocks.compiler import (
    ALLOWED_DIALECTS,
    TEMPLATE_PARAM_KEYS,
    compile_sql,
)
from xiaowei_agent.planning.starrocks.params import SQL_TIME_FORMAT, SlowQueryParams


class SqlGuardError(RuntimeError):
    """AST 校验失败。``rejection`` 给出最具体的拒绝码。

    与 ``BindingError`` 同一模式：拒绝码是闭集枚举成员，消息里不出现任何被检 SQL
    片段——被拒的 SQL 恰恰是最可能携带外部数据的东西。
    """

    def __init__(self, rejection: SqlGuardRejection) -> None:
        super().__init__(rejection.value)
        self.rejection = rejection


_ALLOWED_NODES: Final[frozenset[type[exp.Expression]]] = frozenset(
    {
        exp.Select,
        exp.From,
        exp.Table,
        exp.Identifier,
        exp.Column,
        exp.Where,
        exp.And,
        exp.GTE,
        exp.LT,
        exp.EQ,
        exp.Literal,
        exp.Order,
        exp.Ordered,
        exp.Limit,
        exp.Alias,
        exp.Count,
        exp.Star,
    }
)
"""两条模板实测用到的**全部**节点类型，一个不多。

``exp.Or`` / ``exp.Union`` / ``exp.Subquery`` / ``exp.CTE`` / ``exp.Join`` /
``exp.TableAlias`` / ``exp.Lock`` / ``exp.PropertyEQ`` / ``exp.Parameter`` /
``exp.Into`` / ``exp.Anonymous``（任意函数）全部不在集合内。
"""

_SMART_QUOTES: Final[frozenset[str]] = frozenset("‘’“”")
_FULLWIDTH_RANGE: Final[tuple[int, int]] = (0xFF01, 0xFF5E)


def table_key(table: exp.Table) -> str:
    """表比较的**唯一**口径：``"db.name"``，大小写敏感。

    声明侧（``SqlSurface.allowed_tables``）与校验侧共用本函数的输出格式。两侧各写
    一份是典型缺陷形状：声明是 ``"db.table"``、校验却比 ``exp.Table.db.name``，
    happy path 直接不匹配——而这种不匹配在攻击矩阵下表现为"全部拒绝"，很容易被
    误读成"闸门很严"。

    :raises SqlGuardError: catalog 非空（三段名可指向外部 catalog）或 db 为空
        （裸表名依赖会话默认库，目标不确定）。
    """
    if table.catalog or not table.db:
        raise SqlGuardError(SqlGuardRejection.TABLE_NOT_ALLOWED)
    return f"{table.db}.{table.name}"


def _reject(rejection: SqlGuardRejection) -> SqlGuardError:
    return SqlGuardError(rejection)


def _check_ambiguous_characters(sql: str) -> None:
    """规则 2：歧义字符。

    这一层在设计上不可达（SQL 由我们自己的模板生成），保留它是纵深防御：将来
    模板新增字面量来源时它是最后一道闸。``test_a21_*`` 绕过参数层直接调用本
    模块，证明它确实承重而不是死代码。
    """
    for char in sql:
        if unicodedata.category(char) == "Cf" or char in _SMART_QUOTES:
            raise _reject(SqlGuardRejection.AMBIGUOUS_CHARACTER)
        if _FULLWIDTH_RANGE[0] <= ord(char) <= _FULLWIDTH_RANGE[1]:
            raise _reject(SqlGuardRejection.AMBIGUOUS_CHARACTER)


def _parse_single_statement(sql: str, dialect: str) -> exp.Expression:
    """规则 3：必须解析成功且**恰好**一条语句。

    sqlglot 的 ``ParseError`` 携带被检 SQL 片段，绝不能原样冒泡——它会绕过本模块
    的全部拒绝路径卫生，把外部数据带进调用方的 traceback。
    """
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception:
        # 捕获类型刻意宽：解析器抛什么由第三方决定，而"解析失败"对本模块只有
        # 一种语义。``from None`` 同时切断异常链，避免原始 SQL 随 __cause__ 外泄。
        raise _reject(SqlGuardRejection.UNPARSABLE) from None
    if len(statements) != 1:
        raise _reject(SqlGuardRejection.MULTIPLE_STATEMENTS)
    root = statements[0]
    if not isinstance(root, exp.Expression):
        raise _reject(SqlGuardRejection.UNPARSABLE)
    return root


def _check_nodes(root: exp.Expression, surface: SqlSurface) -> None:
    """规则 5-9、13：逐节点的闭集校验。

    一次遍历同时做多条规则，是为了让"每个节点都被看过"成为结构上的事实：分成
    多次遍历时，新增一条规则很容易漏掉某类节点。
    """
    allowed_table_names = {key.partition(".")[2] for key in surface.allowed_tables}
    for node in root.walk():
        # 规则 9：注释挂在节点属性上，不产生 AST 节点，按节点类型扫永远扫不到。
        if node.comments:
            raise _reject(SqlGuardRejection.COMMENT_PRESENT)
        if type(node) not in _ALLOWED_NODES:
            raise _reject(SqlGuardRejection.FORBIDDEN_NODE)
        if isinstance(node, exp.Star):
            # 规则 8：裸 SELECT * 与 COUNT(*) 都产生 Star，只有父节点能区分。
            if not isinstance(node.parent, exp.Count):
                raise _reject(SqlGuardRejection.STAR_NOT_ALLOWED)
        elif isinstance(node, exp.Table):
            if table_key(node) not in surface.allowed_tables:
                raise _reject(SqlGuardRejection.TABLE_NOT_ALLOWED)
        elif isinstance(node, exp.Column):
            if node.name not in surface.allowed_columns:
                raise _reject(SqlGuardRejection.COLUMN_NOT_ALLOWED)
            qualifier = node.table
            if qualifier and qualifier not in allowed_table_names:
                raise _reject(SqlGuardRejection.COLUMN_NOT_ALLOWED)
        elif isinstance(node, exp.Alias):
            # 规则 13：输出别名是回传给调用方的字段名，必须在声明的闭集内。
            if node.alias not in surface.allowed_output_aliases:
                raise _reject(SqlGuardRejection.COLUMN_NOT_ALLOWED)


def _check_limit(root: exp.Expression, surface: SqlSurface) -> None:
    """规则 10：必须有 LIMIT，且是**非字符串**整数字面量。

    ``LIMIT '20'`` 能被解析，只有 ``is_string`` 能把它与整数区分开。
    """
    limit = root.args.get("limit")
    if not isinstance(limit, exp.Limit):
        raise _reject(SqlGuardRejection.LIMIT_MISSING)
    literal = limit.expression
    if not isinstance(literal, exp.Literal) or literal.is_string:
        raise _reject(SqlGuardRejection.LIMIT_MISSING)
    try:
        value = int(literal.name)
    except ValueError:
        raise _reject(SqlGuardRejection.LIMIT_MISSING) from None
    if not 1 <= value <= surface.max_row_limit:
        raise _reject(SqlGuardRejection.LIMIT_EXCEEDED)


def _window_literal(node: exp.Expression, surface: SqlSurface) -> _dt.datetime | None:
    """从一个比较谓词里取出时间列的字面量；不是时间窗谓词则返回 ``None``。"""
    left, right = node.this, node.expression
    if not isinstance(left, exp.Column) or left.name != surface.time_column:
        return None
    if not isinstance(right, exp.Literal) or not right.is_string:
        return None
    try:
        return _dt.datetime.strptime(right.name, SQL_TIME_FORMAT).replace(tzinfo=_dt.UTC)
    except ValueError:
        return None


def _check_window(root: exp.Expression, params: SlowQueryParams, surface: SqlSurface) -> None:
    """规则 11-12：时间窗必须双边有界、与参数完全一致、且跨度不超上限。"""
    lower: _dt.datetime | None = None
    upper: _dt.datetime | None = None
    for node in root.walk():
        if isinstance(node, exp.GTE):
            lower = _window_literal(node, surface) or lower
        elif isinstance(node, exp.LT):
            upper = _window_literal(node, surface) or upper
    if lower is None or upper is None:
        raise _reject(SqlGuardRejection.WINDOW_UNBOUNDED)
    if (lower, upper) != (params.window_start, params.window_end):
        raise _reject(SqlGuardRejection.WINDOW_MISMATCH)
    if upper - lower > _dt.timedelta(minutes=surface.max_window_minutes):
        raise _reject(SqlGuardRejection.WINDOW_TOO_WIDE)


def verify_sql(
    *, sql: str, params: SlowQueryParams, surface: SqlSurface, template_id: str
) -> None:
    """按闭集规则校验一条 SQL；任一不过即抛 :class:`SqlGuardError`。

    :param sql: 待校验的 SQL。按不可信内容处理——它可能来自被篡改的计划。
    :param params: 已校验参数；窗口比对与重编译都以它为准。
    :param surface: 该 capability 允许触达的 SQL 面。
    :param template_id: 该 SQL 声称由哪条模板编译而来。
    :raises SqlGuardError: 任一规则不通过。异常消息只含闭集拒绝码。
    """
    if template_id not in TEMPLATE_PARAM_KEYS:
        raise _reject(SqlGuardRejection.UNKNOWN_TEMPLATE)
    if surface.dialect not in ALLOWED_DIALECTS:
        raise _reject(SqlGuardRejection.UNKNOWN_DIALECT)
    _check_ambiguous_characters(sql)
    root = _parse_single_statement(sql, surface.dialect)
    if not isinstance(root, exp.Select):
        raise _reject(SqlGuardRejection.NON_SELECT)
    _check_nodes(root, surface)
    _check_limit(root, surface)
    _check_window(root, params, surface)
    # 最后一道闸：SQL 必须是这些参数的确定性产物。plan_hash 能发现整份计划被换，
    # 但 SQL 与参数**同时**被改成自洽的一对时 hash 依然自洽——只有重编译比对能
    # 把这种情形钉死。
    if sql != compile_sql(template_id=template_id, params=params, surface=surface):
        raise _reject(SqlGuardRejection.RECOMPILE_MISMATCH)


# ---- confirmed_readonly（F1，设计 §7.3）----


class _DropSqlglotRecords(logging.Filter):
    """sqlglot 回退为 Command 时把被解析的原文写进 warning。

    本进程里 sqlglot 只处理 SQL，而 SQL 原文不得进入日志（设计 §4 第 1 条），因此丢弃
    ``sqlglot`` logger 的全部记录；解析失败由本模块以闭集拒绝码表达，不依赖它的日志。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return False


logging.getLogger("sqlglot").addFilter(_DropSqlglotRecords())

_READONLY_DIALECT: Final[str] = "starrocks"
_DEFAULT_CATALOG: Final[str] = "default_catalog"

_TOKEN_REJECTIONS: Final[Mapping[TokenScanReason, SqlGuardRejection]] = {
    TokenScanReason.EMPTY: SqlGuardRejection.UNPARSABLE,
    TokenScanReason.TOO_LONG: SqlGuardRejection.UNPARSABLE,
    TokenScanReason.UNTERMINATED: SqlGuardRejection.UNPARSABLE,
    TokenScanReason.INVALID_UTF8: SqlGuardRejection.AMBIGUOUS_CHARACTER,
    TokenScanReason.BOM: SqlGuardRejection.AMBIGUOUS_CHARACTER,
    TokenScanReason.CONTROL_CHARACTER: SqlGuardRejection.AMBIGUOUS_CHARACTER,
    TokenScanReason.AMBIGUOUS_PUNCTUATION: SqlGuardRejection.AMBIGUOUS_CHARACTER,
    TokenScanReason.HINT_COMMENT: SqlGuardRejection.HINT_PRESENT,
    TokenScanReason.MULTI_STATEMENT: SqlGuardRejection.MULTIPLE_STATEMENTS,
    TokenScanReason.INTO_OUTFILE: SqlGuardRejection.NOT_READONLY,
}

_WRITE_NODES: Final[tuple[type[exp.Expr], ...]] = (
    exp.DML,
    exp.DDL,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Comment,
    exp.Refresh,
    exp.Cache,
    exp.Uncache,
    exp.Set,
    exp.Use,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.LoadData,
    exp.Copy,
    exp.Grant,
    exp.Revoke,
    exp.Kill,
    exp.Analyze,
    exp.Into,
    exp.Lock,
    exp.PropertyEQ,
)
"""出现在任何位置都说明不是只读的节点：写入、DDL、会话/事务改写、锁、``:=`` 赋值。

安全不依赖本集合完整：根节点只按允许集合放行，本集合只让拒绝原因更具体，并拦截允许
根节点**内部**的副作用（``INTO``、``FOR UPDATE``、``:=``、CTE 里的 DML）。
"""

_QUERY_ROOTS: Final[tuple[type[exp.Expr], ...]] = (exp.Select, exp.SetOperation)
"""``SetOperation`` 覆盖 ``Union``、``Intersect`` 与 ``Except``。"""

_SHOW_LISTINGS: Final[frozenset[str]] = frozenset(
    {
        "TABLES",
        "TABLE STATUS",
        "CREATE DATABASE",
        "PROCESSLIST",
        "VARIABLES",
        "STATUS",
        "GRANTS",
        "ENGINES",
        "CHARSET",
        "CHARACTER SET",
        "COLLATION",
        "PLUGINS",
        "WARNINGS",
    }
)
"""parser 完整识别、无直接对象的 SHOW。黑名单不提供元数据保密，列举对象名照常放行。"""

_SHOW_RELATION_READS: Final[frozenset[str]] = frozenset(
    {"COLUMNS", "INDEX", "CREATE TABLE", "CREATE VIEW"}
)
"""直接读取一个对象的 SHOW：对象按默认库解析后检查黑名单。"""

_DESCRIBE_TABLE_ARGS: Final[frozenset[str]] = frozenset({"this", "as_json"})
_DESCRIBE_KEYWORDS: Final[frozenset[str]] = frozenset({"DESC", "DESCRIBE"})


@dataclass(frozen=True, slots=True)
class ReadonlyPolicy:
    """一个 target 的只读准入参数：默认库与规范化后的关系黑名单。"""

    default_database: str
    blocked_relation_names: frozenset[QualifiedRelation]


@dataclass(frozen=True, slots=True)
class ReadonlyProof:
    """只读证明。只含路径、语句族与直接对象，**不含**任何 SQL 文本。"""

    path: Literal["ast", "statement_list"]
    statement_family: str
    relations: tuple[QualifiedRelation, ...]


def _catalog_is_internal(catalog: str) -> bool:
    return not catalog or catalog.lower() == _DEFAULT_CATALOG


def _relation(
    *, catalog: str, database: str, name: str, policy: ReadonlyPolicy
) -> QualifiedRelation:
    if not _catalog_is_internal(catalog):
        raise _reject(SqlGuardRejection.EXTERNAL_CATALOG)
    return QualifiedRelation(database=database or policy.default_database, name=name)


def _visible_ctes(table: exp.Table) -> Iterator[str]:
    """``table`` 所在位置可见的 CTE 名。

    非递归 CTE 只对定义在它之后的 CTE 与主查询可见；自身正文与更早的 CTE 正文里同名
    引用的是真实表。这里宁可少认 CTE：少认只会多查一张表，多认会漏查真实表。
    """
    child: exp.Expr = table
    parent = table.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            ctes = list(parent.expressions)
            index = next(i for i, cte in enumerate(ctes) if cte is child)
            visible = ctes[: index + 1] if parent.args.get("recursive") else ctes[:index]
            yield from (cte.alias for cte in visible)
        else:
            with_ = parent.args.get("with_")
            if isinstance(with_, exp.With) and with_ is not child:
                yield from (cte.alias for cte in with_.expressions)
        child, parent = parent, parent.parent


def _is_cte_reference(table: exp.Table) -> bool:
    if table.args.get("db") or table.args.get("catalog"):
        return False
    return table.name in set(_visible_ctes(table))


def _collect_relations(
    root: exp.Expr, policy: ReadonlyPolicy
) -> list[QualifiedRelation]:
    """遍历整棵树：拒绝副作用、table function 与 qualified function，收集基础关系。"""
    relations: list[QualifiedRelation] = []
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            raise _reject(SqlGuardRejection.NOT_READONLY)
        if isinstance(node, exp.Unnest | exp.Explode):
            raise _reject(SqlGuardRejection.TABLE_FUNCTION)
        if isinstance(node, exp.Lateral) and not isinstance(node.this, exp.Subquery):
            raise _reject(SqlGuardRejection.TABLE_FUNCTION)
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise _reject(SqlGuardRejection.QUALIFIED_FUNCTION)
        if isinstance(node, exp.Column) and not _catalog_is_internal(node.catalog):
            raise _reject(SqlGuardRejection.EXTERNAL_CATALOG)
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                raise _reject(SqlGuardRejection.TABLE_FUNCTION)
            if _is_cte_reference(node):
                continue
            relations.append(
                _relation(
                    catalog=node.catalog, database=node.db, name=node.name, policy=policy
                )
            )
    return relations


def _prove_show(
    show: exp.Show, policy: ReadonlyPolicy
) -> tuple[str, list[QualifiedRelation]] | None:
    kind = show.name.upper()
    family = "show." + kind.lower().replace(" ", "_")
    database = show.args.get("db")
    database_name = database.name if isinstance(database, exp.Identifier) else ""
    relations = _collect_relations(show, policy)
    if kind == "DATABASES":
        # SHOW DATABASES FROM <catalog>：这里的 db 参数其实是 Catalog。
        if not _catalog_is_internal(database_name):
            raise _reject(SqlGuardRejection.EXTERNAL_CATALOG)
        return family, relations
    if kind in _SHOW_LISTINGS:
        return family, relations
    if kind in _SHOW_RELATION_READS:
        target = show.args.get("target")
        if not isinstance(target, exp.Identifier):
            return None
        relations.append(
            _relation(catalog="", database=database_name, name=target.name, policy=policy)
        )
        return family, relations
    return None


def _prove_describe(
    describe: exp.Describe, policy: ReadonlyPolicy, *, keyword: str
) -> tuple[str, list[QualifiedRelation]] | None:
    """parser 把 DESC 与 EXPLAIN 都解析成 ``Describe``，并接受 MySQL 的互换写法。

    这里按首个关键字收窄：``DESC``/``DESCRIBE`` 只描述一个对象，``EXPLAIN`` 只包装查询。
    """
    style = describe.args.get("style")
    inner = describe.this
    if keyword in _DESCRIBE_KEYWORDS and isinstance(inner, exp.Table):
        extras = {k for k, v in describe.args.items() if v and k not in _DESCRIBE_TABLE_ARGS}
        if style or extras:
            return None
        return "describe", _collect_relations(describe, policy)
    if keyword != "EXPLAIN":
        return None
    if isinstance(inner, _WRITE_NODES):
        raise _reject(SqlGuardRejection.NOT_READONLY)
    if isinstance(inner, _QUERY_ROOTS):
        if style is None:
            return "explain", _collect_relations(inner, policy)
        if str(style).upper() == "ANALYZE":
            return "explain.analyze", _collect_relations(inner, policy)
    return None


def _prove_ast(
    root: exp.Expr, policy: ReadonlyPolicy, *, keyword: str
) -> tuple[str, list[QualifiedRelation]] | None:
    """AST 路径。能证明则返回（语句族, 关系），形状不在允许集合内返回 ``None``。"""
    if isinstance(root, _QUERY_ROOTS):
        return "query", _collect_relations(root, policy)
    if isinstance(root, exp.Show):
        return _prove_show(root, policy)
    if isinstance(root, exp.Describe):
        return _prove_describe(root, policy, keyword=keyword)
    return None


class SqlParseShape(StrEnum):
    """sqlglot 对一段文本的解析形状；只供 SQL 消息识别（设计 §5.1），**不是**只读证明。"""

    STATEMENTS = "statements"
    """一条或多条完整的非 ``Command`` 语句。"""
    COMMAND = "command"
    """至少一条被 sqlglot 降级为 ``Command``：认得语句关键字，但不建模其语法。"""
    TOO_COMPLEX = "too_complex"
    """嵌套过深等非语法原因导致解析器放弃。"""
    INVALID = "invalid"
    """语法或词法错误、没有语句，或只有关键字的 SELECT。"""


def sql_parse_shape(text: str) -> SqlParseShape:
    """按 sqlglot 的结论给文本分类；任何异常都不外泄原文。"""
    try:
        parsed = sqlglot.parse(text, read=_READONLY_DIALECT)
    except (ParseError, TokenError):
        return SqlParseShape.INVALID
    except Exception:
        return SqlParseShape.TOO_COMPLEX
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
    # 单独一个 SELECT 关键字会被解析成没有投影的 Select，它不是完整语句。
    if not statements or any(
        isinstance(statement, exp.Select) and not statement.expressions
        for statement in statements
    ):
        return SqlParseShape.INVALID
    if any(isinstance(statement, exp.Command) for statement in statements):
        return SqlParseShape.COMMAND
    return SqlParseShape.STATEMENTS


def parses_as_complete_statements(text: str) -> bool:
    """sqlglot 能否把文本完整解析为一条或多条非 ``Command`` 语句（嵌入 SQL 检测用）。"""
    return sql_parse_shape(text) is SqlParseShape.STATEMENTS


def parses_as_query(text: str) -> bool:
    """sqlglot 能否把文本完整解析为恰好一条真正的查询（``Query``：SELECT/UNION/WITH）。

    供 SQL 消息识别 fail-closed 用，**不是**只读证明。解析器因嵌套过深等非语法原因放弃时按
    查询处理：放弃的一方只能多拒绝，不能让 SQL 进模型。
    """
    try:
        parsed = sqlglot.parse(text, read=_READONLY_DIALECT)
    except (ParseError, TokenError):
        return False
    except Exception:
        return True
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return False
    query = statements[0]
    # 单独一个 SELECT 关键字会被解析成没有投影的 Select，它不是完整语句。
    return not isinstance(query, exp.Select) or bool(query.expressions)


def _parse_statement(text: str) -> exp.Expr | None:
    """解析单条语句；parser 不认识（ParseError / TokenError）返回 ``None`` 交清单路径。"""
    try:
        parsed = sqlglot.parse(text, read=_READONLY_DIALECT)
    except (ParseError, TokenError):
        return None
    except Exception:
        # 递归过深等非语法失败没有“交清单”的语义，直接拒绝；切断异常链防止原文外泄。
        raise _reject(SqlGuardRejection.UNPARSABLE) from None
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1:
        raise _reject(SqlGuardRejection.MULTIPLE_STATEMENTS)
    return statements[0]


def _scan(raw: bytes) -> TokenScan:
    try:
        return scan_sql(raw)
    except TokenScanError as exc:
        reason = exc.reason
    # 在 except 块外抛出：扫描异常不留在 __context__ 里。
    raise _reject(_TOKEN_REJECTIONS[reason])


def _statement_text(scan: TokenScan) -> str:
    """去掉首尾注释与尾随分号后的语句文本，只用于分析；执行的永远是原始 bytes。"""
    tokens = scan.statement_tokens
    return scan.raw[tokens[0].start_byte : tokens[-1].end_byte].decode()


def _prove_statement_list(
    matched: StatementMatch, scan: TokenScan, policy: ReadonlyPolicy
) -> list[QualifiedRelation]:
    """清单路径：解析清单项提取出的对象；带内层查询的项把内层字节交 AST 路径证明。"""
    if matched.inner_query is not None:
        start, end = matched.inner_query
        inner = _parse_statement(scan.raw[start:end].decode())
        if isinstance(inner, _WRITE_NODES):
            raise _reject(SqlGuardRejection.NOT_READONLY)
        if not isinstance(inner, _QUERY_ROOTS):
            raise _reject(SqlGuardRejection.READONLY_STATEMENT_NOT_SUPPORTED)
        return _collect_relations(inner, policy)
    return [
        _relation(catalog=ref.catalog, database=ref.database, name=ref.name, policy=policy)
        for ref in matched.relations
    ]


def verify_confirmed_readonly(
    *, raw: bytes, expected_sha256: str, policy: ReadonlyPolicy
) -> ReadonlyProof:
    """证明一条用户提交的 SQL 只读，并返回它直接读取的对象。

    顺序：原文 hash → token 扫描 → 单语句解析 → AST 路径（parser 完整识别时）或代码内
    只读语句清单 → 内部 Catalog 与黑名单。本函数不改写、不格式化 SQL，也不返回任何 SQL
    文本；adapter 执行的仍是 ``raw``。

    :raises SqlGuardError: 任一规则不通过；消息只含闭集拒绝码。
    """
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise _reject(SqlGuardRejection.HASH_MISMATCH)
    scan = _scan(raw)
    root = _parse_statement(_statement_text(scan))
    keyword = scan.text(scan.statement_tokens[0]).upper()
    proven = _prove_ast(root, policy, keyword=keyword) if root is not None else None
    path: Literal["ast", "statement_list"] = "ast"
    if proven is None:
        matched = match_statement(scan)
        if matched is None:
            if isinstance(root, _WRITE_NODES) and not isinstance(root, exp.Command):
                raise _reject(SqlGuardRejection.NOT_READONLY)
            raise _reject(SqlGuardRejection.READONLY_STATEMENT_NOT_SUPPORTED)
        path = "statement_list"
        proven = matched.family, _prove_statement_list(matched, scan, policy)
    family, relations = proven
    for relation in relations:
        if relation in policy.blocked_relation_names:
            raise _reject(SqlGuardRejection.BLOCKED_RELATION)
    return ReadonlyProof(
        path=path,
        statement_family=family,
        relations=tuple(dict.fromkeys(relations)),
    )

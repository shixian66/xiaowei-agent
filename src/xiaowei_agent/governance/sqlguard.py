"""SQL AST 校验：闭集白名单 + 重编译比对。

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
import unicodedata
from typing import Final

import sqlglot
from sqlglot import exp

from xiaowei_agent.contracts import SqlGuardRejection, SqlSurface
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

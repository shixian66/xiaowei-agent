"""代码内只读语句清单（设计 §7.3 清单路径，计划 §1 A1–A12、B1–B9）。

sqlglot 30.17.0 降级为 ``Command`` 或抛 ParseError 的 StarRocks 只读语句只认本清单。每一项是
一条**完整匹配**的 token 文法：清单没写的子句、多余或重复的 token 都不匹配，因此不存在
“以 SHOW 开头即放行”的泛化规则，也不需要另列禁止子句。文法按 ``TokenScan.statement_tokens``
匹配（注释与尾随分号已去掉），关键字大小写不敏感，标识符可用反引号。

直接对象由 ``TargetRule`` 声明、由捕获唯一提取；同一条语句若有多种完整匹配（无论来自同一项
还是不同项）即视为歧义，返回 ``None``，由 SQLGuard 以“暂未支持”拒绝。

清单随代码版本走；升级 StarRocks 大版本时人工核对。登记支持不等于给账号增权。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from xiaowei_agent.governance.sql_tokens import SqlToken, TokenKind, TokenScan

_Captures = tuple[tuple[str, object], ...]
_Matches = Iterator[tuple[int, _Captures]]


@dataclass(frozen=True, slots=True)
class _Input:
    scan: TokenScan
    tokens: tuple[SqlToken, ...]

    def word(self, pos: int) -> str | None:
        if pos < len(self.tokens) and self.tokens[pos].kind is TokenKind.WORD:
            return self.scan.text(self.tokens[pos]).upper()
        return None

    def identifier(self, pos: int) -> str | None:
        if pos >= len(self.tokens):
            return None
        token = self.tokens[pos]
        text = self.scan.text(token)
        if token.kind is TokenKind.WORD:
            return text
        if token.kind is TokenKind.QUOTED_IDENTIFIER:
            inner = text[1:-1].replace("``", "`")
            return inner or None
        return None

    def symbol(self, pos: int) -> str | None:
        if pos < len(self.tokens) and self.tokens[pos].kind is TokenKind.SYMBOL:
            return self.scan.text(self.tokens[pos])
        return None


class _Element:
    """文法元素：从 ``pos`` 起产生所有可能的 (下一位置, 捕获)。"""

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        raise NotImplementedError

    def capture_names(self) -> Iterator[tuple[str, bool]]:
        """(捕获名, 是否必有)。"""
        return iter(())


@dataclass(frozen=True, slots=True)
class Kw(_Element):
    """连续的关键字。"""

    words: tuple[str, ...]

    def __init__(self, *words: str) -> None:
        object.__setattr__(self, "words", words)

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        for offset, word in enumerate(self.words):
            if source.word(pos + offset) != word:
                return
        yield pos + len(self.words), captures


@dataclass(frozen=True, slots=True)
class Sym(_Element):
    symbol: str

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        if source.symbol(pos) == self.symbol:
            yield pos + 1, captures


@dataclass(frozen=True, slots=True)
class Name(_Element):
    """``a``、``a.b`` 或 ``a.b.c``，捕获为各段的元组。"""

    capture: str
    min_parts: int = 1
    max_parts: int = 1

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        parts: list[str] = []
        cursor = pos
        while len(parts) < self.max_parts:
            if parts:
                if source.symbol(cursor) != ".":
                    break
                cursor += 1
            part = source.identifier(cursor)
            if part is None:
                break
            parts.append(part)
            cursor += 1
            if len(parts) >= self.min_parts:
                yield cursor, (*captures, (self.capture, tuple(parts)))

    def capture_names(self) -> Iterator[tuple[str, bool]]:
        yield self.capture, True


@dataclass(frozen=True, slots=True)
class Str(_Element):
    """字符串字面量。含反斜杠或引号转义的字面量不匹配：避免在这里重新实现反转义。"""

    capture: str | None = None

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        if pos >= len(source.tokens) or source.tokens[pos].kind is not TokenKind.STRING:
            return
        text = source.scan.text(source.tokens[pos])
        inner = text[1:-1]
        if not inner or "\\" in inner or text[0] in inner:
            return
        if self.capture is None:
            yield pos + 1, captures
        else:
            yield pos + 1, (*captures, (self.capture, inner))

    def capture_names(self) -> Iterator[tuple[str, bool]]:
        if self.capture is not None:
            yield self.capture, True


@dataclass(frozen=True, slots=True)
class Int(_Element):
    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        word = source.word(pos)
        if word is not None and word.isdigit():
            yield pos + 1, captures


@dataclass(frozen=True, slots=True)
class Column(_Element):
    """登记列名之一（大小写不敏感）；用于只接受登记列的 WHERE。"""

    allowed: frozenset[str]

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        name = source.identifier(pos)
        if name is not None and name.upper() in self.allowed:
            yield pos + 1, captures


@dataclass(frozen=True, slots=True)
class Rest(_Element):
    """剩余全部 token（至少一个），捕获其原始字节区间。"""

    capture: str

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        if pos < len(source.tokens):
            span = (source.tokens[pos].start_byte, source.tokens[-1].end_byte)
            yield len(source.tokens), (*captures, (self.capture, span))

    def capture_names(self) -> Iterator[tuple[str, bool]]:
        yield self.capture, True


def _match_sequence(
    elements: tuple[_Element, ...], source: _Input, pos: int, captures: _Captures
) -> _Matches:
    if not elements:
        yield pos, captures
        return
    head, tail = elements[0], elements[1:]
    for next_pos, next_captures in head.match(source, pos, captures):
        yield from _match_sequence(tail, source, next_pos, next_captures)


@dataclass(frozen=True, slots=True)
class Opt(_Element):
    elements: tuple[_Element, ...]

    def __init__(self, *elements: _Element) -> None:
        object.__setattr__(self, "elements", elements)

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        yield pos, captures
        yield from _match_sequence(self.elements, source, pos, captures)

    def capture_names(self) -> Iterator[tuple[str, bool]]:
        for element in self.elements:
            for name, _required in element.capture_names():
                yield name, False


@dataclass(frozen=True, slots=True)
class OneOf(_Element):
    alternatives: tuple[tuple[_Element, ...], ...]

    def __init__(self, *alternatives: tuple[_Element, ...]) -> None:
        object.__setattr__(self, "alternatives", alternatives)

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        for alternative in self.alternatives:
            yield from _match_sequence(alternative, source, pos, captures)

    def capture_names(self) -> Iterator[tuple[str, bool]]:
        for alternative in self.alternatives:
            for element in alternative:
                for name, _required in element.capture_names():
                    yield name, False


@dataclass(frozen=True, slots=True)
class Many(_Element):
    """零次或多次。

    不允许捕获：重复捕获无法唯一提取对象。展开是迭代的——64 KiB 内可以重复几千次，
    递归展开会耗尽调用栈。
    """

    elements: tuple[_Element, ...]

    def __init__(self, *elements: _Element) -> None:
        if any(True for element in elements for _ in element.capture_names()):
            raise RuntimeError("Many must not capture")
        object.__setattr__(self, "elements", elements)

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        pending = [pos]
        seen = {pos}
        while pending:
            current = pending.pop()
            yield current, captures
            for next_pos, _ in _match_sequence(self.elements, source, current, captures):
                if next_pos > current and next_pos not in seen:
                    seen.add(next_pos)
                    pending.append(next_pos)


class TargetRule(StrEnum):
    """一条清单项的直接对象提取规则。"""

    NONE = "none"
    """无直接对象（系统或库内 listing）；可捕获 ``database``，不作为对象目标。"""
    RELATION = "relation"
    """必有一个 ``relation``（1–3 段，三段时 Catalog 须为内部 Catalog）。"""
    OPTIONAL_RELATION = "optional_relation"
    """可选 ``relation`` 或 ``relation_name``（按 ``database`` 或默认库解析）。"""
    INNER_QUERY = "inner_query"
    """必有 ``inner_query``：内层查询的原始字节区间，交 AST 路径证明。"""


@dataclass(frozen=True, slots=True)
class ReadonlyStatement:
    family: str
    pattern: tuple[_Element, ...]
    target: TargetRule


@dataclass(frozen=True, slots=True)
class RelationRef:
    """清单路径提取出的对象引用，尚未按默认库与 Catalog 规则解析。"""

    catalog: str
    database: str
    name: str


@dataclass(frozen=True, slots=True)
class StatementMatch:
    family: str
    relations: tuple[RelationRef, ...]
    inner_query: tuple[int, int] | None


_FROM_OR_IN: Final = OneOf((Kw("FROM"),), (Kw("IN"),))
_OPT_DB: Final = Opt(_FROM_OR_IN, Name("database"))
_OPT_FROM_DB: Final = Opt(Kw("FROM"), Name("database"))
_RELATION: Final = Name("relation", 1, 3)


def _where_registered(*columns: str) -> Opt:
    predicate = (Column(frozenset(columns)), Sym("="), OneOf((Str(),), (Int(),)))
    return Opt(Kw("WHERE"), *predicate, Many(Kw("AND"), *predicate))


def _show(*words: str) -> Kw:
    return Kw("SHOW", *words)


READONLY_STATEMENTS: Final[tuple[ReadonlyStatement, ...]] = (
    # ---- A. 必选 ----
    ReadonlyStatement(
        "show.create_materialized_view",
        (_show("CREATE", "MATERIALIZED", "VIEW"), _RELATION),
        TargetRule.RELATION,
    ),
    ReadonlyStatement(
        "show.materialized_views",
        (
            _show("MATERIALIZED", "VIEWS"),
            _OPT_DB,
            Opt(
                OneOf(
                    (Kw("LIKE"), Str()),
                    (Kw("WHERE", "NAME"), Sym("="), Str("relation_name")),
                    (Kw("WHERE", "NAME", "LIKE"), Str()),
                )
            ),
        ),
        TargetRule.OPTIONAL_RELATION,
    ),
    ReadonlyStatement(
        "show.partitions",
        (Kw("SHOW"), Opt(Kw("TEMPORARY")), Kw("PARTITIONS", "FROM"), _RELATION),
        TargetRule.RELATION,
    ),
    ReadonlyStatement("show.tablet", (_show("TABLET", "FROM"), _RELATION), TargetRule.RELATION),
    ReadonlyStatement("show.tablet_id", (_show("TABLET"), Int()), TargetRule.NONE),
    ReadonlyStatement(
        "show.data",
        (
            _show("DATA"),
            Opt(Kw("FROM"), OneOf((Name("database"),), (Name("relation", 2, 2),))),
        ),
        TargetRule.OPTIONAL_RELATION,
    ),
    ReadonlyStatement("show.load", (_show("LOAD"), _OPT_FROM_DB), TargetRule.NONE),
    ReadonlyStatement(
        "show.routine_load", (_show("ROUTINE", "LOAD"), _OPT_FROM_DB), TargetRule.NONE
    ),
    ReadonlyStatement(
        "show.functions",
        (Kw("SHOW"), Opt(Kw("FULL")), Kw("FUNCTIONS"), _OPT_DB, Opt(Kw("LIKE"), Str())),
        TargetRule.NONE,
    ),
    ReadonlyStatement("show.catalogs", (_show("CATALOGS"),), TargetRule.NONE),
    ReadonlyStatement("show.frontends", (_show("FRONTENDS"),), TargetRule.NONE),
    ReadonlyStatement("show.backends", (_show("BACKENDS"),), TargetRule.NONE),
    ReadonlyStatement("show.resource_groups", (_show("RESOURCE", "GROUPS"),), TargetRule.NONE),
    ReadonlyStatement("show.proc", (_show("PROC"), Str()), TargetRule.NONE),
    ReadonlyStatement(
        "show.profilelist", (_show("PROFILELIST"), Opt(Kw("LIMIT"), Int())), TargetRule.NONE
    ),
    ReadonlyStatement(
        "show.alter_table",
        (
            _show("ALTER", "TABLE"),
            OneOf((Kw("COLUMN"),), (Kw("ROLLUP"),), (Kw("MATERIALIZED", "VIEW"),)),
            _OPT_DB,
            Opt(Kw("WHERE", "TABLENAME"), Sym("="), Str("relation_name")),
        ),
        TargetRule.OPTIONAL_RELATION,
    ),
    ReadonlyStatement(
        "admin.show_replica_status",
        (Kw("ADMIN", "SHOW", "REPLICA", "STATUS", "FROM"), _RELATION),
        TargetRule.RELATION,
    ),
    ReadonlyStatement(
        "admin.show_replica_distribution",
        (Kw("ADMIN", "SHOW", "REPLICA", "DISTRIBUTION", "FROM"), _RELATION),
        TargetRule.RELATION,
    ),
    ReadonlyStatement(
        "analyze.profile", (Kw("ANALYZE", "PROFILE", "FROM"), Str()), TargetRule.NONE
    ),
    ReadonlyStatement(
        "explain.modifier",
        (
            Kw("EXPLAIN"),
            OneOf((Kw("LOGICAL"),), (Kw("VERBOSE"),), (Kw("COSTS"),)),
            Rest("inner_query"),
        ),
        TargetRule.INNER_QUERY,
    ),
    # ---- B. 常用只读语句 ----
    ReadonlyStatement("show.running_queries", (_show("RUNNING", "QUERIES"),), TargetRule.NONE),
    ReadonlyStatement(
        "show.analyze_status",
        (
            _show("ANALYZE", "STATUS"),
            _where_registered("ID", "DATABASE", "TABLE", "TYPE", "STATUS"),
        ),
        TargetRule.NONE,
    ),
    ReadonlyStatement(
        "show.stats_meta",
        (_show("STATS", "META"), _where_registered("DATABASE", "TABLE", "TYPE")),
        TargetRule.NONE,
    ),
    ReadonlyStatement(
        "show.dynamic_partition_tables",
        (_show("DYNAMIC", "PARTITION", "TABLES"), _OPT_DB),
        TargetRule.NONE,
    ),
    ReadonlyStatement("show.delete", (_show("DELETE"), _OPT_FROM_DB), TargetRule.NONE),
    ReadonlyStatement("show.export", (_show("EXPORT"), _OPT_FROM_DB), TargetRule.NONE),
    ReadonlyStatement(
        "show.transaction",
        (_show("TRANSACTION"), _OPT_FROM_DB, Kw("WHERE", "ID"), Sym("="), Int()),
        TargetRule.NONE,
    ),
    ReadonlyStatement("show.compute_nodes", (_show("COMPUTE", "NODES"),), TargetRule.NONE),
    ReadonlyStatement("show.roles", (_show("ROLES"),), TargetRule.NONE),
    ReadonlyStatement(
        "show.create_routine_load",
        (_show("CREATE", "ROUTINE", "LOAD"), Name("job", 1, 2)),
        TargetRule.NONE,
    ),
    ReadonlyStatement(
        "describe.all",
        (OneOf((Kw("DESC"),), (Kw("DESCRIBE"),)), _RELATION, Kw("ALL")),
        TargetRule.RELATION,
    ),
)

_RELATION_CAPTURES: Final = frozenset({"relation", "relation_name"})


def _check_target_rule(statement: ReadonlyStatement) -> None:
    """导入时校验：文法的捕获必须与声明的提取规则一致。"""
    names: dict[str, bool] = {}
    for element in statement.pattern:
        for name, required in element.capture_names():
            names[name] = names.get(name, False) or required
    relation_captures = {n: r for n, r in names.items() if n in _RELATION_CAPTURES}
    rule = statement.target
    consistent = {
        TargetRule.NONE: not relation_captures and "inner_query" not in names,
        TargetRule.RELATION: relation_captures == {"relation": True}
        and "inner_query" not in names,
        TargetRule.OPTIONAL_RELATION: bool(relation_captures)
        and not any(relation_captures.values())
        and "inner_query" not in names,
        TargetRule.INNER_QUERY: names == {"inner_query": True},
    }[rule]
    if not consistent:
        raise RuntimeError("readonly statement pattern does not match its target rule")


for _statement in READONLY_STATEMENTS:
    _check_target_rule(_statement)
if len({s.family for s in READONLY_STATEMENTS}) != len(READONLY_STATEMENTS):
    raise RuntimeError("readonly statement families must be unique")


def _relation_ref(parts: tuple[str, ...]) -> RelationRef:
    padded = ("", "", *parts)[-3:]
    return RelationRef(catalog=padded[0], database=padded[1], name=padded[2])


def _to_match(statement: ReadonlyStatement, captures: _Captures) -> StatementMatch:
    values = dict(captures)
    relations: list[RelationRef] = []
    relation = values.get("relation")
    if isinstance(relation, tuple):
        relations.append(_relation_ref(relation))
    relation_name = values.get("relation_name")
    if isinstance(relation_name, str):
        database = values.get("database")
        relations.append(
            RelationRef(
                catalog="",
                database=database[0] if isinstance(database, tuple) else "",
                name=relation_name,
            )
        )
    inner = values.get("inner_query")
    return StatementMatch(
        family=statement.family,
        relations=tuple(relations),
        inner_query=inner if isinstance(inner, tuple) else None,
    )


def match_statement(scan: TokenScan) -> StatementMatch | None:
    """按清单完整匹配一条语句。无匹配或有多种不同匹配（歧义）时返回 ``None``。"""
    source = _Input(scan=scan, tokens=scan.statement_tokens)
    matches: set[StatementMatch] = set()
    for statement in READONLY_STATEMENTS:
        for end, captures in _match_sequence(statement.pattern, source, 0, ()):
            if end == len(source.tokens):
                matches.add(_to_match(statement, captures))
    if len(matches) != 1:
        return None
    return matches.pop()


GrammarElement = _Element
"""文法元素的公开名：识别登记表（``sql_statements``）用同一套元素写完整形状。"""


@dataclass(frozen=True, slots=True)
class AnyStr(_Element):
    """任意字符串字面量（含转义）；只用于识别登记表，不捕获、不反转义。"""

    def match(self, source: _Input, pos: int, captures: _Captures) -> _Matches:
        if pos < len(source.tokens) and source.tokens[pos].kind is TokenKind.STRING:
            yield pos + 1, captures


def matches_completely(scan: TokenScan, pattern: tuple[_Element, ...]) -> bool:
    """同一文法元素的完整匹配（全部 statement tokens 恰好耗尽）；供识别登记表复用。"""
    source = _Input(scan=scan, tokens=scan.statement_tokens)
    return any(
        end == len(source.tokens) for end, _ in _match_sequence(pattern, source, 0, ())
    )

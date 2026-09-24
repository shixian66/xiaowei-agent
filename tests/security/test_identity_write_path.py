"""授权事实与审计事实的**写入口冻结**——用 AST 钉，因为行为证不了。

三组守卫，一个共同理由：**多出来的那条写路径不会让任何现有用例变红**。
再开一个方法写 ``user_accounts``、再写一条 INSERT 直连 ``admin_audit_events``、
在别处给 ``local_admins.user_id`` 赋一次值——三件事都不改变任何已有断言的结果，
却各自足以让"每一次授权改变都带着一条派生出的审计"从事实退回成一句注释。

守卫**按属性判定，不按写法枚举**：扫的是"这条语句写了哪张表、写了哪几列"，不是
"代码里出现了哪个函数名"。按名字冻结的名单，下一个新写法自动逃出去。

允许项一律 owner-qualified 到 ``相对路径::类.方法``，**不跳过整模块**：模块粒度的
允许等于"这个文件里怎么写都行"，而绕过恰恰发生在文件内部。

**残余风险，如实记：** 静态判不出写了哪一列的形式（变量键、``**`` 展开、``Any``
注解、别名导入、``sa.text`` 拼的 SQL）本文件一律**报警**而不是放行，但"报警"意味
着它需要有人来看；它不等于这些形式被封死。``persistence/migrations/`` 不在扫描
范围内——迁移是冻结的历史快照，整表建/删，由 ``require_destructive_authorization``
另行把关。
"""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

from xiaowei_agent.persistence import schema

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_PERSISTENCE = _SRC / "persistence"

_WRITE_VERBS = frozenset({"insert", "update", "delete"})
_TABLE_BY_CONSTANT = {
    name: value.name
    for name, value in vars(schema).items()
    if isinstance(value, sa.Table)
}

AUTHZ_TABLES = frozenset(
    {"user_accounts", "user_role_assignments", "external_identities"}
)
ACTIVATION_TABLES = frozenset({"activation_requests"})
AUDIT_TABLE = "admin_audit_events"
CREDENTIAL_TABLE = "local_admins"
CREDENTIAL_LINK_COLUMN = "user_id"

_AUTHZ_WRITE_SITES = frozenset(
    {
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_in_transaction",
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_managed_user_change",
        "persistence/postgres.py::PostgresUserDirectoryStore._insert_account",
        "persistence/postgres.py::PostgresUserDirectoryStore._upsert_role",
        "persistence/postgres.py::PostgresUserDirectoryStore._bind_subject",
    }
)
"""``user_accounts`` / ``user_role_assignments`` / ``external_identities`` 的全部写点。

``apply()`` 本身不发 SQL——它开事务并委托，所以名单里是助手而不是 ``apply``。
受管写点单列，是为了让状态/角色 CAS 的 SQL 不能被悄悄搬到第二条路径。
"""

_ACTIVATION_SQL_WRITE_SITES = frozenset(
    {
        "persistence/postgres.py::PostgresActivationStore.create_or_reuse",
        "persistence/postgres.py::PostgresUserDirectoryStore._decide_activation",
        "persistence/postgres.py::PostgresActivationRetentionStore.purge_expired_terminal",
    }
)
"""激活表的三类 SQL 写点：创建/收割、目录事务内的终态决策、W5 固定期终态保留。"""

_ACTIVATION_MEMORY_WRITE_SITES = frozenset(
    {
        "persistence/memory.py::InMemoryPersistenceState.__init__",
        "persistence/fake.py::InMemoryActivationStore.create_or_reuse",
        "persistence/fake.py::InMemoryUserDirectoryStore._approve_activation",
        "persistence/fake.py::InMemoryUserDirectoryStore._reject_activation",
        "persistence/fake.py::InMemoryUserDirectoryStore._restore",
        "persistence/fake.py::InMemoryActivationRetentionStore.purge_expired_terminal",
    }
)
"""内存激活事实的创建/收割、终态决策、事务回滚恢复点与固定期终态保留。"""

_AUDIT_WRITE_SITES = frozenset({"persistence/postgres.py::_insert_audit_event"})
"""**单元素**。

**不写成** ``_AUTHZ_WRITE_SITES | {...}``：那等于顺带允许四个目录助手直接写审计表，
而它们现在全都走 ``_insert_audit_event``。把不需要的允许项留在名单里，守卫就比它能
守的范围松一圈，而松出来的那一圈正好是"绕过唯一落库口"。
"""

_CREDENTIAL_LINK_SITES = frozenset(
    {"persistence/postgres.py::PostgresUserDirectoryStore._bootstrap_in_transaction"}
)
"""``local_admins.user_id`` 的唯一写点。

这一列是**授权事实**，不是凭据：改密路径照常写 ``password_hash`` 与
``must_change_password``，不受本名单约束，也不该受。
"""

_FULL_CANDIDATE_HELPERS = frozenset(
    {
        "persistence/admin_audit.py::seal",
        "persistence/fake.py::InMemoryAdminAuditStore._append_locked",
        "persistence/fake.py::InMemoryUserDirectoryStore._write_audit",
        "persistence/postgres.py::PostgresAdminAuditStore._append_candidate",
        "persistence/postgres.py::PostgresUserDirectoryStore._audit",
        "persistence/postgres.py::_insert_audit_event",
    }
)
"""能收下一条**完整审计候选**的函数，用 ``==`` 冻结。

``==`` 而不是 ``<=``：新增一个就必须显式加进来，加的时候必须回答"它的调用点是谁"。

``admin_audit_event_to_row`` **不在**这里——它收的是 ``AdminAuditEvent``（一条已带
``event_id`` 与 ``created_at`` 的事实，不是候选）。它确实能把任意事件摊成列，但真正
落库要经过 ``sa.insert``，那已由 :data:`_AUDIT_WRITE_SITES` 冻结；两处都列一遍就是
重复校验。

**六个而不是三个。** 计划写的是三个，写实现时才看清另外三个同样收得下完整候选：
两个目录侧的包装（把"阶段已占用"翻译成 ``AdminAuditUnwritableError``）和一个审计侧
的包装。它们都是真实存在的候选入口，按 ``==`` 的规矩必须显式列出来并回答调用点。
把它们藏起来才是这条守卫最怕的事。
"""

_HELPER_CALL_SITES = frozenset(
    {
        "persistence/fake.py::InMemoryAdminAuditStore._append_locked",
        "persistence/fake.py::InMemoryAdminAuditStore.append_denied",
        "persistence/fake.py::InMemoryAdminAuditStore.append_started",
        "persistence/fake.py::InMemoryAdminAuditStore.append_terminal",
        "persistence/fake.py::InMemoryUserDirectoryStore._assign_role",
        "persistence/fake.py::InMemoryUserDirectoryStore._change_managed_role",
        "persistence/fake.py::InMemoryUserDirectoryStore._approve_activation",
        "persistence/fake.py::InMemoryUserDirectoryStore._bind_identity",
        "persistence/fake.py::InMemoryUserDirectoryStore._bootstrap",
        "persistence/fake.py::InMemoryUserDirectoryStore._create_user",
        "persistence/fake.py::InMemoryUserDirectoryStore._migrate",
        "persistence/fake.py::InMemoryUserDirectoryStore._reject_activation",
        "persistence/fake.py::InMemoryUserDirectoryStore._revoke_role",
        "persistence/fake.py::InMemoryUserDirectoryStore._set_status",
        "persistence/fake.py::InMemoryUserDirectoryStore._set_managed_status",
        "persistence/fake.py::InMemoryUserDirectoryStore._unbind_identity",
        "persistence/fake.py::InMemoryUserDirectoryStore._write_audit",
        "persistence/postgres.py::PostgresAdminAuditStore._append_candidate",
        "persistence/postgres.py::PostgresAdminAuditStore.append_denied",
        "persistence/postgres.py::PostgresAdminAuditStore.append_started",
        "persistence/postgres.py::PostgresAdminAuditStore.append_terminal",
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_in_transaction",
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_managed_user_change",
        "persistence/postgres.py::PostgresUserDirectoryStore._audit",
        "persistence/postgres.py::PostgresUserDirectoryStore._bootstrap_in_transaction",
        "persistence/postgres.py::PostgresUserDirectoryStore._decide_activation",
        "persistence/postgres.py::_insert_audit_event",
    }
)
""":data:`_FULL_CANDIDATE_HELPERS` 里每一个 helper 的全部调用位置。

冻结调用点而不只冻结 helper 本身：helper 是窄的，但**谁能叫它**同样是写权限。

不在这条说明里写死条数：数字写两遍，改的人只会改一处，而剩下那一处会让下一个
读者以为实现越了界。真正的判定是下面那条 ``==``。
"""


@dataclass(frozen=True)
class WriteSite:
    """一处对某张表的写，连同它写了哪几列。"""

    module: str
    owner: str
    table: str | None
    verb: str
    columns: frozenset[str] | None

    @property
    def qualified(self) -> str:
        return f"{self.module}::{self.owner}"

    @property
    def is_unprovable(self) -> bool:
        """**目标表**静态判不出来。报警，不是放行。

        只看表名而不看列名，是因为列名判不出来在本仓库是常态：``rows.py`` 的
        ``X_to_row`` 配 ``values(**row)`` 是既有约定，十余处都这么写。列的粒度只在
        ``local_admins`` 上才有意义（那里要分清"改口令"和"改链接"），而那一格由
        :meth:`touches` 处理——它对判不出的写法返回 ``True``，也就是按"写到了"论。
        """
        return self.table is None

    def touches(self, column: str) -> bool:
        return self.columns is None or column in self.columns


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _annotate_owners(tree: ast.Module) -> None:
    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                child_owner = child.name
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                child_owner = f"{owner}.{child.name}" if owner else child.name
            else:
                child_owner = owner
            child._owner = child_owner  # type: ignore[attr-defined]
            visit(child, child_owner)

    visit(tree, "")


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[child] = node
    return mapping


def _columns_of(call: ast.Call) -> frozenset[str] | None:
    """``values(...)`` 实际写入的列名；判不出来返回 ``None``。

    四种等价写法都要认：``values(user_id=v)``、``values({T.c.user_id: v})``、
    ``values({"user_id": v})``、``values({T.c["user_id"]: v})``。
    """
    names: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg is None:
            return None  # ``**expr`` 展开：判不出写了哪几列
        names.add(keyword.arg)
    for argument in call.args:
        if not isinstance(argument, ast.Dict):
            return None
        for key in argument.keys:
            if key is None:
                return None  # ``{**expr}``
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
            elif isinstance(key, ast.Attribute):
                names.add(key.attr)
            elif (
                isinstance(key, ast.Subscript)
                and isinstance(key.slice, ast.Constant)
                and isinstance(key.slice.value, str)
            ):
                names.add(key.slice.value)
            else:
                return None
    return frozenset(names)


def _enclosing_statement(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.AST | None:
    current: ast.AST | None = node
    while current is not None and not isinstance(current, ast.stmt):
        current = parents.get(current)
    return current


def write_sites(source: str, *, module: str) -> tuple[WriteSite, ...]:
    """扫出一份源码里对数据库表的全部写入，连同它们写了哪几列。

    这是**唯一**一份判定逻辑：下面每一条反证都调用它本身，而不是另抄一遍。抄一遍
    的反证只能证明"抄出来的那份会抓"，抓不抓真守卫无从得知。
    """
    tree = ast.parse(source)
    _annotate_owners(tree)
    parents = _parents(tree)
    found: list[WriteSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        owner = getattr(node, "_owner", "")
        verb: str | None = None
        table: str | None = None
        if isinstance(node.func, ast.Attribute) and node.func.attr in _WRITE_VERBS:
            base = _dotted(node.func.value)
            if base is None or base.split(".")[0] != "sa":
                continue
            verb = node.func.attr
        elif isinstance(node.func, ast.Name) and node.func.id in _WRITE_VERBS:
            # 别名导入（``from sqlalchemy import insert``）：认不出目标表，报警。
            verb = node.func.id
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "text"
            and _dotted(node.func.value) == "sa"
        ):
            literal = node.args[0] if node.args else None
            text = (
                literal.value.lower()
                if isinstance(literal, ast.Constant)
                and isinstance(literal.value, str)
                else None
            )
            if text is None or any(word in text for word in _WRITE_VERBS):
                # 拼出来的 SQL 判不出写了什么；空 SELECT 之外一律报警。
                if text is None or any(word in text for word in _WRITE_VERBS):
                    found.append(
                        WriteSite(module, owner, None, "text", None)
                    )
            continue
        else:
            continue
        if node.args:
            table = _TABLE_BY_CONSTANT.get(_dotted(node.args[0]) or "")
        columns: frozenset[str] | None = frozenset()
        if verb == "delete":
            # DELETE 拿走整行，因此它写到了每一列，包括链接列。
            columns = None if table is None else frozenset(
                schema.METADATA.tables[table].columns.keys()
            )
        else:
            statement = _enclosing_statement(node, parents)
            values_calls = [
                child
                for child in ast.walk(statement)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "values"
            ] if statement is not None else []
            if len(values_calls) != 1:
                columns = None if values_calls else frozenset()
            else:
                columns = _columns_of(values_calls[0])
        found.append(WriteSite(module, owner, table, verb, columns))
    return tuple(found)


def candidate_helpers(source: str, *, module: str) -> frozenset[str]:
    """按**签名**发现能收下 ``AdminAuditCandidate`` 的函数。

    按签名而不是按名字：改个函数名就逃出名单的守卫，守不住任何东西。
    """
    tree = ast.parse(source)
    _annotate_owners(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = [*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs]
        for argument in arguments:
            annotation = argument.annotation
            if annotation is None:
                continue
            name = (
                annotation.value
                if isinstance(annotation, ast.Constant)
                else _dotted(annotation)
            )
            if isinstance(name, str) and name.split(".")[-1] == "AdminAuditCandidate":
                found.add(f"{module}::{getattr(node, '_owner', node.name)}")
                break
    return frozenset(found)


def helper_call_sites(source: str, *, module: str, helpers: frozenset[str]) -> frozenset[str]:
    """上面那些 helper 的调用位置。

    名单项是 ``相对路径::类.方法``，而路径里本身带点号——用 ``rsplit(".")`` 去取
    "函数名"会得到 ``py::xxx`` 这种垃圾。因此这里先按 ``::`` 切，再取最后一段。
    """
    names = {item.split("::", 1)[1].rsplit(".", 1)[-1] for item in helpers}
    tree = ast.parse(source)
    _annotate_owners(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else None
        )
        if called in names:
            found.add(f"{module}::{getattr(node, '_owner', '')}")
    return frozenset(found)


def activation_memory_write_sites(source: str, *, module: str) -> frozenset[str]:
    """发现对 ``state.activation_requests`` 的赋值或原地改写。"""
    tree = ast.parse(source)
    _annotate_owners(tree)
    found: set[str] = set()

    def is_activation_state(node: ast.AST) -> bool:
        dotted = _dotted(node)
        return dotted is not None and dotted.endswith(".activation_requests")

    for node in ast.walk(tree):
        target: ast.AST | None = None
        if isinstance(node, ast.Assign):
            for candidate in node.targets:
                base = candidate.value if isinstance(candidate, ast.Subscript) else candidate
                if is_activation_state(base):
                    target = candidate
                    break
        elif isinstance(node, ast.AnnAssign):
            candidate = node.target
            base = candidate.value if isinstance(candidate, ast.Subscript) else candidate
            if is_activation_state(base):
                target = candidate
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"clear", "update", "pop", "setdefault"}
            and is_activation_state(node.func.value)
        ):
            target = node.func.value
        if target is not None:
            found.add(f"{module}::{getattr(node, '_owner', '')}")
    return frozenset(found)


def _persistence_modules() -> dict[str, str]:
    """只扫 ``persistence/*.py``，不递归进 ``migrations/``。见模块 docstring。"""
    return {
        f"persistence/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted(_PERSISTENCE.glob("*.py"))
    }


def _all_sites() -> tuple[WriteSite, ...]:
    return tuple(
        site
        for module, source in _persistence_modules().items()
        for site in write_sites(source, module=module)
    )


# ---- 真实仓库上的判定 -------------------------------------------------------


def test_authorization_tables_are_written_only_at_frozen_sites() -> None:
    offenders = {
        site.qualified
        for site in _all_sites()
        if site.table in AUTHZ_TABLES and site.qualified not in _AUTHZ_WRITE_SITES
    }
    assert not offenders, offenders


def test_activation_table_is_written_only_at_its_frozen_sql_owners() -> None:
    discovered = {
        site.qualified for site in _all_sites() if site.table in ACTIVATION_TABLES
    }
    assert discovered == _ACTIVATION_SQL_WRITE_SITES


def test_activation_memory_facts_are_written_only_at_frozen_owners() -> None:
    discovered: set[str] = set()
    for module, source in _persistence_modules().items():
        discovered |= activation_memory_write_sites(source, module=module)
    assert discovered == _ACTIVATION_MEMORY_WRITE_SITES


def test_audit_table_is_written_only_at_frozen_sites() -> None:
    offenders = {
        site.qualified
        for site in _all_sites()
        if site.table == AUDIT_TABLE and site.qualified not in _AUDIT_WRITE_SITES
    }
    assert not offenders, offenders


def test_the_credential_link_is_written_only_at_frozen_sites() -> None:
    offenders = {
        site.qualified
        for site in _all_sites()
        if site.table == CREDENTIAL_TABLE
        and site.touches(CREDENTIAL_LINK_COLUMN)
        and site.qualified not in _CREDENTIAL_LINK_SITES
    }
    assert not offenders, offenders


def test_no_write_in_persistence_hides_which_table_it_targets() -> None:
    """判不出**目标表**的写法一律报警——别名导入、``sa.text`` 拼的 SQL 都算。

    它们不是被放行的，只是静态分析回答不了，因此必须有人来看。当前仓库里应当一条
    都没有；出现一条就要么改写法，要么在这里说明它为什么不可判且可接受。

    列名判不出来的写法不在这里：见 :meth:`WriteSite.is_unprovable` 的说明，它们由
    ``local_admins`` 那条按列判定的守卫按"写到了"论处。
    """
    unprovable = {
        f"{site.qualified} ({site.verb})" for site in _all_sites() if site.is_unprovable
    }
    assert not unprovable, unprovable


def test_the_frozen_sites_are_still_allowed() -> None:
    """**正常对照**：名单里的站点确实存在，而且确实在写它该写的东西。

    没有这一条，把三份名单全清空、把实现里的写全删掉，上面三条会同时全绿。
    """
    sites = _all_sites()
    authz = {site.qualified for site in sites if site.table in AUTHZ_TABLES}
    audit = {site.qualified for site in sites if site.table == AUDIT_TABLE}
    link = {
        site.qualified
        for site in sites
        if site.table == CREDENTIAL_TABLE and site.touches(CREDENTIAL_LINK_COLUMN)
    }
    assert authz == _AUTHZ_WRITE_SITES
    assert audit == _AUDIT_WRITE_SITES
    assert link == _CREDENTIAL_LINK_SITES


def test_credential_writes_that_are_not_the_link_column_are_not_flagged() -> None:
    """只改 ``password_hash`` 的正常路径不得误报——改密是凭据，不是授权。"""
    change_password = [
        site
        for site in _all_sites()
        if site.table == CREDENTIAL_TABLE
        and site.owner == "PostgresLocalAdminStore.change_password_and_rotate_session"
    ]
    assert change_password, "改密路径不见了——守卫扫描坏了"
    assert all(not site.touches(CREDENTIAL_LINK_COLUMN) for site in change_password)


def test_the_full_candidate_helpers_are_exactly_the_declared_ones() -> None:
    discovered: set[str] = set()
    for module, source in _persistence_modules().items():
        discovered |= candidate_helpers(source, module=module)
    assert discovered == _FULL_CANDIDATE_HELPERS


def test_the_helper_names_really_are_function_names() -> None:
    """名单项的解析本身必须正确。

    路径里带点号（``persistence/fake.py``），直接 ``rsplit(".")`` 会得到 ``py`` 这种
    垃圾名，于是调用点扫描永远扫不到任何东西——而"扫不到"与"没有违规"长得一模一样。
    """
    names = {
        item.split("::", 1)[1].rsplit(".", 1)[-1] for item in _FULL_CANDIDATE_HELPERS
    }
    assert names == {
        "seal",
        "_append_locked",
        "_write_audit",
        "_append_candidate",
        "_audit",
        "_insert_audit_event",
    }
    assert not any(name.startswith("py") for name in names)


def test_the_full_candidate_helpers_have_only_declared_call_sites() -> None:
    discovered: set[str] = set()
    for module, source in _persistence_modules().items():
        discovered |= helper_call_sites(
            source, module=module, helpers=_FULL_CANDIDATE_HELPERS
        )
    assert discovered == _HELPER_CALL_SITES


# ---- 反证：每一条都调用**守卫本身** -----------------------------------------

_MODULE_LEVEL_WRITE = """
import sqlalchemy as sa
from xiaowei_agent.persistence.schema import USER_ACCOUNTS

async def sneak(connection):
    await connection.execute(
        sa.insert(USER_ACCOUNTS).values(user_id="x", actor="y")
    )
"""

_PRIVATE_METHOD_WRITE = """
import sqlalchemy as sa
from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS

class Sneaky:
    async def _quietly_append(self, connection):
        await connection.execute(sa.insert(ADMIN_AUDIT_EVENTS).values(event_id="x"))
"""


def test_the_guard_catches_a_module_level_write_function() -> None:
    """恢复的旧缺陷：在 store 之外另开一个模块级函数直接写授权表。"""
    sites = write_sites(_MODULE_LEVEL_WRITE, module="persistence/sneak.py")
    offenders = {
        site.qualified
        for site in sites
        if site.table in AUTHZ_TABLES and site.qualified not in _AUTHZ_WRITE_SITES
    }
    assert offenders == {"persistence/sneak.py::sneak"}


def test_the_activation_sql_guard_catches_a_new_write_owner() -> None:
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import ACTIVATION_REQUESTS\n"
        "async def sneak(connection):\n"
        "    await connection.execute(\n"
        "        sa.update(ACTIVATION_REQUESTS).values(status='approved')\n"
        "    )\n"
    )
    discovered = {
        site.qualified
        for site in write_sites(source, module="persistence/sneak.py")
        if site.table in ACTIVATION_TABLES
    }
    assert discovered - _ACTIVATION_SQL_WRITE_SITES == {
        "persistence/sneak.py::sneak"
    }


def test_the_activation_memory_guard_catches_a_new_write_owner() -> None:
    source = (
        "class Sneaky:\n"
        "    def decide(self, request):\n"
        "        self._state.activation_requests[request.request_id] = request\n"
    )
    discovered = activation_memory_write_sites(
        source, module="persistence/sneak.py"
    )
    assert discovered - _ACTIVATION_MEMORY_WRITE_SITES == {
        "persistence/sneak.py::Sneaky.decide"
    }


def test_the_guard_catches_a_new_private_write_method() -> None:
    """恢复的旧缺陷：在类里新开一个私有方法绕过唯一落库口写审计表。"""
    sites = write_sites(_PRIVATE_METHOD_WRITE, module="persistence/sneak.py")
    offenders = {
        site.qualified
        for site in sites
        if site.table == AUDIT_TABLE and site.qualified not in _AUDIT_WRITE_SITES
    }
    assert offenders == {"persistence/sneak.py::Sneaky._quietly_append"}


_EQUIVALENT_LINK_WRITES = (
    'sa.update(LOCAL_ADMINS).values(user_id="x")',
    'sa.update(LOCAL_ADMINS).values({LOCAL_ADMINS.c.user_id: "x"})',
    'sa.update(LOCAL_ADMINS).values({"user_id": "x"})',
    'sa.update(LOCAL_ADMINS).values({LOCAL_ADMINS.c["user_id"]: "x"})',
    "sa.delete(LOCAL_ADMINS)",
)


@pytest.mark.parametrize("expression", _EQUIVALENT_LINK_WRITES)
def test_every_equivalent_credential_link_write_is_caught(expression: str) -> None:
    """五种等价写法必须全部命中，``DELETE`` 也算——它拿走整行，链接一并消失。"""
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import LOCAL_ADMINS\n"
        "async def sneak(connection):\n"
        f"    await connection.execute({expression})\n"
    )
    sites = write_sites(source, module="persistence/sneak.py")
    offenders = {
        site.qualified
        for site in sites
        if site.table == CREDENTIAL_TABLE
        and site.touches(CREDENTIAL_LINK_COLUMN)
        and site.qualified not in _CREDENTIAL_LINK_SITES
    }
    assert offenders == {"persistence/sneak.py::sneak"}


_UNPROVABLE_WRITES = (
    "sa.update(LOCAL_ADMINS).values(**payload)",
    "sa.update(LOCAL_ADMINS).values({column: 'x'})",
    'sa.text("UPDATE local_admins SET user_id = :u")',
)


@pytest.mark.parametrize("expression", _UNPROVABLE_WRITES)
def test_an_unprovable_credential_write_is_reported_not_waved_through(
    expression: str,
) -> None:
    """静态判不出写了哪一列的形式**按"写到了链接列"论处**，不是放行。

    放行才是危险的那一边：``values(**payload)`` 想写哪一列就写哪一列。
    """
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import LOCAL_ADMINS\n"
        "async def sneak(connection, payload, column):\n"
        f"    await connection.execute({expression})\n"
    )
    sites = write_sites(source, module="persistence/sneak.py")
    assert sites
    offenders = {
        site.qualified
        for site in sites
        if site.touches(CREDENTIAL_LINK_COLUMN)
        and site.qualified not in _CREDENTIAL_LINK_SITES
    }
    assert offenders == {"persistence/sneak.py::sneak"}


def test_a_credential_write_that_skips_the_link_column_is_not_flagged() -> None:
    """反证的**正常对照**：只改口令的那种写法不能被上面几条顺手抓进来。"""
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import LOCAL_ADMINS\n"
        "async def rotate(connection):\n"
        "    await connection.execute(\n"
        "        sa.update(LOCAL_ADMINS).values(password_hash='h', updated_at=None)\n"
        "    )\n"
    )
    sites = write_sites(source, module="persistence/sneak.py")
    assert sites
    assert not any(site.touches(CREDENTIAL_LINK_COLUMN) for site in sites)


def test_the_credential_guard_catches_a_wrong_owner_inside_an_allowed_module() -> None:
    """owner 粒度而不是模块粒度：允许模块里的**另一个**方法照样被抓。"""
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import LOCAL_ADMINS\n"
        "class PostgresUserDirectoryStore:\n"
        "    async def _somewhere_else(self, connection):\n"
        "        await connection.execute(\n"
        "            sa.update(LOCAL_ADMINS).values(user_id='x')\n"
        "        )\n"
    )
    sites = write_sites(source, module="persistence/postgres.py")
    offenders = {
        site.qualified
        for site in sites
        if site.table == CREDENTIAL_TABLE
        and site.touches(CREDENTIAL_LINK_COLUMN)
        and site.qualified not in _CREDENTIAL_LINK_SITES
    }
    assert offenders == {
        "persistence/postgres.py::PostgresUserDirectoryStore._somewhere_else"
    }


_NEW_CANDIDATE_TAKER = """
from xiaowei_agent.contracts.admin_audit import AdminAuditCandidate

class Sneaky:
    def _stamp(self, candidate: AdminAuditCandidate) -> None:
        return None
"""


def test_the_helper_discovery_finds_a_newly_added_candidate_taker() -> None:
    """恢复的旧缺陷：新加一个能收下完整候选的函数而不登记。"""
    discovered = candidate_helpers(_NEW_CANDIDATE_TAKER, module="persistence/sneak.py")
    assert discovered == {"persistence/sneak.py::Sneaky._stamp"}
    assert discovered != _FULL_CANDIDATE_HELPERS


_UNAUTHORISED_CALL_SITE = """
class Elsewhere:
    async def forge(self, connection, candidate):
        return await _insert_audit_event(connection, candidate, now=None)
"""

_DECLARED_CALL_SITE = """
class PostgresUserDirectoryStore:
    async def _audit(self, connection, candidate):
        return await _insert_audit_event(connection, candidate, now=None)
"""


def test_an_unauthorised_call_site_of_the_write_sink_is_caught() -> None:
    """恢复的旧缺陷：唯一落库口还在，但多了一个不该叫它的调用方。"""
    discovered = helper_call_sites(
        _UNAUTHORISED_CALL_SITE,
        module="persistence/sneak.py",
        helpers=_FULL_CANDIDATE_HELPERS,
    )
    assert discovered - _HELPER_CALL_SITES == {"persistence/sneak.py::Elsewhere.forge"}


def test_a_declared_call_site_of_the_write_sink_is_allowed() -> None:
    """上一条的**正常对照**：登记过的调用点不得被判成违规。"""
    discovered = helper_call_sites(
        _DECLARED_CALL_SITE,
        module="persistence/postgres.py",
        helpers=_FULL_CANDIDATE_HELPERS,
    )
    assert discovered <= _HELPER_CALL_SITES
    assert discovered

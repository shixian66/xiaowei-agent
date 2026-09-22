"""审计表 append-only —— ``persistence/`` 里不存在改写它的语句。

**这条没有独立的行为反证。** 只要 store 上没有改写方法，任何"表其实可以被改写"的
缺陷都从 Protocol 上完全观察不到；写一条行为用例去证明"改不了"，只能证明"我没调到
改它的接口"。因此改用机械检查——与 ``test_audit_ledger_guards.py`` 同一处境，同一处置。

真正危险的不是今天的代码，而是明天那条"顺手加一个清理任务"的 PR：审计表会长，
长到某一天有人想加保留期。那条 PR 不会让任何用例变红，除非有这个文件。

扫描范围与 ``test_identity_write_path.py`` 相同：``persistence/*.py``，不含
``migrations/``（迁移是冻结的历史快照，整表建/删，另有授权闸门）。
"""

import inspect

import pytest
from tests.security.test_identity_write_path import (
    AUDIT_TABLE,
    _persistence_modules,
    write_sites,
)

from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.fake import InMemoryAdminAuditStore
from xiaowei_agent.persistence.postgres import PostgresAdminAuditStore

pytestmark = pytest.mark.security

_MUTATING_VERBS = frozenset({"update", "delete"})

_FORBIDDEN_WORDS = (
    "update",
    "delete",
    "purge",
    "prune",
    "truncate",
    "retention",
    "expire",
    "clean",
    "vacuum",
    "rotate",
    "archive",
)


def _mutating_audit_sites(source: str, *, module: str) -> set[str]:
    return {
        site.qualified
        for site in write_sites(source, module=module)
        if site.table == AUDIT_TABLE and site.verb in _MUTATING_VERBS
    }


def test_no_persistence_module_updates_or_deletes_admin_audit_events() -> None:
    offenders: set[str] = set()
    for module, source in _persistence_modules().items():
        offenders |= _mutating_audit_sites(source, module=module)
    assert not offenders, offenders


_MUTATING_SNIPPETS = (
    "sa.update(ADMIN_AUDIT_EVENTS).values(outcome='succeeded')",
    "sa.delete(ADMIN_AUDIT_EVENTS)",
)


@pytest.mark.parametrize("expression", _MUTATING_SNIPPETS)
def test_the_guard_actually_detects_a_mutating_call(expression: str) -> None:
    """上一条的反证。

    恢复的旧缺陷正是那条"顺手加的保留期清理"：一个改写或删除审计行的语句，谁也不会
    因为它而变红。这里把它塞进守卫本身（不是抄一份判定逻辑），确认确实被抓。
    """
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS\n"
        "async def housekeeping(connection):\n"
        f"    await connection.execute({expression})\n"
    )
    assert _mutating_audit_sites(source, module="persistence/sneak.py") == {
        "persistence/sneak.py::housekeeping"
    }


def test_the_guard_does_not_flag_an_insert() -> None:
    """反证的**正常对照**：唯一合法的那个动词不能被顺手抓进来。

    没有这条，把 ``_MUTATING_VERBS`` 写成"全部动词"也会让上面两条同时全绿，而那样
    的守卫会把正常的落库口一起判成违规——护栏太紧和太松一样，最后都会被关掉。
    """
    source = (
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS\n"
        "async def append(connection):\n"
        "    await connection.execute(\n"
        "        sa.insert(ADMIN_AUDIT_EVENTS).values(event_id='x')\n"
        "    )\n"
    )
    assert _mutating_audit_sites(source, module="persistence/sneak.py") == set()
    assert write_sites(source, module="persistence/sneak.py")


@pytest.mark.parametrize(
    "implementation",
    [AdminAuditStore, InMemoryAdminAuditStore, PostgresAdminAuditStore],
    ids=lambda item: item.__name__,
)
def test_audit_store_offers_no_cleanup_or_retention_api(implementation: type) -> None:
    """Protocol 与两个实现的公开表面上都不许出现清理、保留期或改写的意思。

    按**词**判而不是按精确名单判：下一个人想加的那个方法不会叫
    ``delete_old_events``，会叫 ``prune`` 或 ``apply_retention``。
    """
    public = [
        name
        for name in dir(implementation)
        if not name.startswith("_") and callable(getattr(implementation, name, None))
    ]
    assert public, "表面是空的——扫描坏了"
    offenders = [
        name for name in public if any(word in name.lower() for word in _FORBIDDEN_WORDS)
    ]
    assert not offenders, offenders


def test_the_forbidden_word_list_would_catch_a_plausible_retention_method() -> None:
    """上一条的反证：真要有人加，他会起什么名字。"""
    plausible = (
        "prune_old_events",
        "apply_retention",
        "delete_before",
        "purge",
        "truncate_audit",
        "expire_events",
        "archive_events",
        "vacuum",
        "rotate_audit_log",
        "cleanup",
        "update_event",
    )
    for name in plausible:
        assert any(word in name.lower() for word in _FORBIDDEN_WORDS), name


def test_the_write_methods_take_narrow_types_not_a_full_candidate() -> None:
    """三个窄写方法各自只收自己那个类型，没有一个能收下完整候选。

    通用的 ``append(candidate)`` 之所以不可接受，正是因为它能独立声称一次目录成功；
    这条断言让"某天把参数类型放宽成候选"这件事必须先经过这里。
    """
    expected = {
        "append_started": "AdminAuditStart",
        "append_terminal": "AdminAuditTerminal",
        "append_denied": "AdminAuditDenial",
    }
    for method, annotation in expected.items():
        for implementation in (
            AdminAuditStore,
            InMemoryAdminAuditStore,
            PostgresAdminAuditStore,
        ):
            parameters = inspect.signature(getattr(implementation, method)).parameters
            payload = [name for name in parameters if name != "self"]
            assert len(payload) == 1, f"{implementation.__name__}.{method}"
            declared = parameters[payload[0]].annotation
            name = getattr(declared, "__name__", str(declared))
            assert name == annotation, f"{implementation.__name__}.{method}: {name}"

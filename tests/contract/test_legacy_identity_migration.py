"""旧身份一次性迁移：整批原子、skip 判据 fail-closed、上限两端闭合。

这批用例全部跑在内存实现上。它钉的是**迁移这一层**的判断——映射、skip 判据、
批次组装与错误转换；"授权改变必带同事务审计"那条不变量由切片 B 的共享套件在
内存与 PostgreSQL 两侧各钉一遍，这里不重复。
"""

import json
from pathlib import Path

import pytest

from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.enums import ProductRole, UserStatus
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    CreateUserCommand,
    DirectoryCommand,
)
from xiaowei_agent.interfaces.legacy_identity_migration import (
    LegacyMigrationConflictError,
    LegacyMigrationReport,
    legacy_user_id,
    migrate_static_identities,
)
from xiaowei_agent.persistence.fake import InMemoryUserDirectoryStore

_TENANT = "dev-local"
_ENVIRONMENT = "dev"
_ACTOR_USER_ID = "local-admin"
_ACTOR = "admin"


@pytest.fixture
def directory(clock, memory_state):
    return InMemoryUserDirectoryStore(clock=clock, state=memory_state)


def _entry(subject_ref: str, actor: str, *labels: str) -> dict[str, object]:
    return {"subject_ref": subject_ref, "actor": actor, "labels": list(labels)}


def _document(tmp_path: Path, *entries: dict[str, object]) -> str:
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": _TENANT,
                "environment_id": _ENVIRONMENT,
                "entries": list(entries),
            }
        ),
        encoding="utf-8",
    )
    return str(path)


async def _migrate(path: str, directory) -> LegacyMigrationReport:
    return await migrate_static_identities(
        document_path=path,
        directory=directory,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT,
        actor_user_id=_ACTOR_USER_ID,
        actor=_ACTOR,
    )


def _context(operation_id: str) -> AdminOperationContext:
    from xiaowei_agent.contracts.enums import IdentitySource

    return AdminOperationContext(
        operation_id=operation_id,
        actor_user_id=_ACTOR_USER_ID,
        actor=_ACTOR,
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


async def _seed(directory, command: DirectoryCommand, operation_id: str) -> None:
    """用**目录自己的**写路径预建既有事实，不绕过 ``apply()``。"""
    await directory.apply(command=command, context=_context(operation_id))


def _facts(memory_state) -> dict[str, int]:
    return {
        "accounts": len(memory_state.user_accounts),
        "roles": len(memory_state.user_role_assignments),
        "bindings": len(memory_state.external_identities),
        "audits": len(memory_state.admin_audit_events),
    }


@pytest.mark.parametrize(
    ("label", "role"),
    [
        ("admin", ProductRole.ADMIN),
        ("operator", ProductRole.OPERATOR),
        ("dba", ProductRole.OPERATOR),
        ("oncall", ProductRole.OPERATOR),
        ("viewer", ProductRole.USER),
        ("approver", ProductRole.USER),
    ],
)
async def test_labels_map_to_product_roles_per_spec_6_6(
    tmp_path: Path, directory, label: str, role: ProductRole
) -> None:
    path = _document(tmp_path, _entry("subject-alice", "alice", label))

    report = await _migrate(path, directory)

    assert report.created == 1
    facts = await directory.load_account(
        user_id=legacy_user_id(actor="alice"),
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT,
    )
    assert facts is not None
    assert facts.assignment.role is role


async def test_viewer_and_approver_both_become_plain_users(
    tmp_path: Path, directory
) -> None:
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "viewer"),
        _entry("subject-bob", "bob", "approver"),
    )

    report = await _migrate(path, directory)

    for actor in ("alice", "bob"):
        facts = await directory.load_account(
            user_id=legacy_user_id(actor=actor),
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
        )
        assert facts is not None
        assert facts.assignment.role is ProductRole.USER
    # approver 当前没有 ACL 真源，只进报告，不生效任何高于 USER 的授权。
    assert report.deferred_labels == (("bob", "approver"),)


async def test_multiple_labels_take_the_highest_role(
    tmp_path: Path, directory
) -> None:
    path = _document(
        tmp_path, _entry("subject-alice", "alice", "viewer", "dba", "admin")
    )

    await _migrate(path, directory)

    facts = await directory.load_account(
        user_id=legacy_user_id(actor="alice"),
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT,
    )
    assert facts is not None
    assert facts.assignment.role is ProductRole.ADMIN


async def test_rerunning_the_migration_changes_nothing(
    tmp_path: Path, directory, memory_state
) -> None:
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "operator"),
        _entry("subject-bob", "bob", "viewer"),
    )
    first = await _migrate(path, directory)
    after_first = _facts(memory_state)

    second = await _migrate(path, directory)

    assert first.created == 2 and first.skipped == 0
    assert second.created == 0 and second.skipped == 2
    assert second.audit_event_ids == ()
    assert _facts(memory_state) == after_first


async def test_a_database_conflict_leaves_zero_rows_behind(
    tmp_path: Path, directory, memory_state
) -> None:
    """冲突必须制造在**数据库既有事实**上。

    在输入文件里放两个相同 actor 测到的是解析器——那种文档在解析期就被拒了，
    根本走不到 ``apply()``，也就证明不了整批原子。
    """
    await _seed(
        directory,
        CreateUserCommand(
            user_id="someone-else",
            actor="bob",
            display_name="Bob",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.USER,
        ),
        "seed-actor-taken",
    )
    before = _facts(memory_state)
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "operator"),
        _entry("subject-bob", "bob", "viewer"),
    )

    with pytest.raises(LegacyMigrationConflictError):
        await _migrate(path, directory)

    # 批次里合法的那一条也没有留下来。
    assert _facts(memory_state) == before
    assert await directory.load_account(
        user_id=legacy_user_id(actor="alice"),
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT,
    ) is None


async def test_every_migrated_entry_writes_its_own_audit_event(
    tmp_path: Path, directory, memory_state
) -> None:
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "operator"),
        _entry("subject-bob", "bob", "viewer"),
        _entry("subject-carol", "carol", "admin"),
    )

    report = await _migrate(path, directory)

    assert len(report.audit_event_ids) == 3
    assert len(set(report.audit_event_ids)) == 3
    events = [
        memory_state.admin_audit_events[event_id]
        for event_id in report.audit_event_ids
    ]
    assert {event.action.value for event in events} == {"legacy_identity_migrated"}
    assert {event.outcome.value for event in events} == {"succeeded"}
    assert [event.effect.role for event in events] == [
        ProductRole.OPERATOR,
        ProductRole.USER,
        ProductRole.ADMIN,
    ]
    assert {event.effect.status for event in events} == {UserStatus.ACTIVE}
    # 每条一个自己的 operation_id：共用一个就写不下三条终态事件。
    assert len({event.operation_id for event in events}) == 3


async def test_a_256_character_actor_migrates_whole(
    tmp_path: Path, directory
) -> None:
    actor = "a" * 256
    path = _document(tmp_path, _entry("subject-long", actor, "operator"))

    report = await _migrate(path, directory)

    assert report.created == 1
    user_id = legacy_user_id(actor=actor)
    assert len(user_id) <= 64
    facts = await directory.load_account(
        user_id=user_id, tenant_id=_TENANT, environment_id=_ENVIRONMENT
    )
    assert facts is not None
    # actor 是身份，逐字搬过去；截断会把两个人合成一个。
    assert facts.account.actor == actor
    # display_name 是纯显示字段，是唯一允许截断的那一个。
    assert facts.account.display_name == actor[:128]
    assert len(facts.account.display_name) == 128


async def test_an_actor_beyond_the_contract_bound_writes_zero_rows(
    tmp_path: Path, directory, memory_state
) -> None:
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "operator"),
        _entry("subject-long", "a" * 257, "operator"),
    )

    with pytest.raises(LegacyMigrationConflictError):
        await _migrate(path, directory)

    assert _facts(memory_state) == {
        "accounts": 0,
        "roles": 0,
        "bindings": 0,
        "audits": 0,
    }


async def test_the_bound_failure_happens_before_any_apply(
    tmp_path: Path, directory
) -> None:
    """上一条红在**命令组装期**，不是"写到一半"。"""

    class _CountingDirectory:
        def __init__(self, inner) -> None:
            self._inner = inner
            self.applied = 0

        async def load_account(self, **kwargs):
            return await self._inner.load_account(**kwargs)

        async def resolve_by_subject(self, **kwargs):
            return await self._inner.resolve_by_subject(**kwargs)

        async def apply(self, **kwargs):
            self.applied += 1
            return await self._inner.apply(**kwargs)

    counting = _CountingDirectory(directory)
    path = _document(tmp_path, _entry("subject-long", "a" * 257, "operator"))

    with pytest.raises(LegacyMigrationConflictError):
        await _migrate(path, counting)

    assert counting.applied == 0


async def test_report_never_contains_a_plaintext_open_id(
    tmp_path: Path, directory
) -> None:
    path = _document(tmp_path, _entry("subject-alice", "alice", "approver"))

    report = await _migrate(path, directory)

    assert "subject-alice" not in repr(report)
    assert "subject-alice" not in report.model_dump_json()
    assert "subject-alice" not in json.dumps(report.model_dump(mode="json"))


async def test_an_account_without_its_binding_is_a_conflict_not_a_skip(
    tmp_path: Path, directory, memory_state
) -> None:
    """不变量 2 的"绑定缺失"一格。

    账号、actor、状态、角色四项**全部按正确值建好**，只让绑定一项缺席；否则它
    可能是被别的判据挡住的，删掉绑定检查它照样绿。
    """
    await _seed(
        directory,
        CreateUserCommand(
            user_id=legacy_user_id(actor="alice"),
            actor="alice",
            display_name="alice",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.OPERATOR,
        ),
        "seed-no-binding",
    )
    before = _facts(memory_state)
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "operator"),
        _entry("subject-bob", "bob", "viewer"),
    )

    with pytest.raises(LegacyMigrationConflictError):
        await _migrate(path, directory)

    assert _facts(memory_state) == before


async def test_a_role_that_no_longer_matches_is_a_conflict(
    tmp_path: Path, directory, memory_state
) -> None:
    """不变量 2 的"角色不符"一格。

    账号、actor、状态**和飞书绑定**全部按正确值建好，只让角色一项不同——只预建
    账号而不建绑定的版本测的是"绑定缺失"，不是它名字说的那件事。
    """
    user_id = legacy_user_id(actor="alice")
    await _seed(
        directory,
        CreateUserCommand(
            user_id=user_id,
            actor="alice",
            display_name="alice",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.OPERATOR,
        ),
        "seed-role-drift-1",
    )
    await _seed(
        directory,
        BindExternalIdentityCommand(
            user_id=user_id,
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            subject_ref="subject-alice",
        ),
        "seed-role-drift-2",
    )
    await _seed(
        directory,
        AssignRoleCommand(
            user_id=user_id,
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.USER,
        ),
        "seed-role-drift-3",
    )
    before = _facts(memory_state)
    path = _document(
        tmp_path,
        _entry("subject-alice", "alice", "operator"),
        _entry("subject-bob", "bob", "viewer"),
    )

    with pytest.raises(LegacyMigrationConflictError):
        await _migrate(path, directory)

    assert _facts(memory_state) == before

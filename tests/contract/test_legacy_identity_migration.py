"""旧身份一次性迁移：整批原子、skip 判据 fail-closed、上限两端闭合。

这批用例全部跑在内存实现上。它钉的是**迁移这一层**的判断——映射、skip 判据、
批次组装与错误转换；"授权改变必带同事务审计"那条不变量由切片 B 的共享套件在
内存与 PostgreSQL 两侧各钉一遍，这里不重复。
"""

import json
from pathlib import Path

import pytest

from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.base import CONTROLLED_PII_MAX_LENGTH
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole, UserStatus
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    CreateUserCommand,
    DirectoryCommand,
    SetUserStatusCommand,
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


def _document(
    tmp_path: Path, *entries: dict[str, object], environment_id: str = _ENVIRONMENT
) -> str:
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": _TENANT,
                "environment_id": environment_id,
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


async def test_a_subject_ref_at_the_contract_bound_migrates_whole(
    tmp_path: Path, directory
) -> None:
    """上限之内**完整迁移**。

    这个上限不是随便取的：飞书事件 DTO 与 OAuth 交换结果都按同一个上限收 `open_id`，
    目录比它们窄一个字符，就会有一批入口收得下、目录绑不进去的真实身份。
    """
    subject = "o" * CONTROLLED_PII_MAX_LENGTH
    path = _document(tmp_path, _entry(subject, "alice", "operator"))

    report = await _migrate(path, directory)

    assert report.created == 1
    facts = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT,
        subject_ref=subject,
    )
    assert facts is not None
    assert facts.account.user_id == legacy_user_id(actor="alice")


@pytest.mark.parametrize(
    ("name", "make"),
    [
        (
            "actor",
            lambda tmp: _document(
                tmp,
                _entry("subject-alice", "alice", "operator"),
                _entry("subject-long", "a" * 257, "operator"),
            ),
        ),
        (
            "subject_ref",
            lambda tmp: _document(
                tmp,
                _entry("subject-alice", "alice", "operator"),
                _entry("o" * (CONTROLLED_PII_MAX_LENGTH + 1), "bob", "operator"),
            ),
        ),
        (
            "environment_id",
            lambda tmp: _document(
                tmp, _entry("subject-alice", "alice", "operator"), environment_id="e" * 65
            ),
        ),
    ],
)
async def test_a_value_beyond_the_contract_bound_writes_zero_rows(
    tmp_path: Path, directory, memory_state, name: str, make
) -> None:
    """三个字段的上限之外都是**整批零写入**，而且都是同一个领域错误。

    ``environment_id`` 那一格是上一轮漏掉的：entry 的组装被收敛了，命令与上下文的
    组装没有，于是原始的 pydantic ``ValidationError`` 直接漏给调用方——同一件事
    （这批装不进契约）却有两种错误分类。
    """
    path = make(tmp_path)
    environment_id = "e" * 65 if name == "environment_id" else _ENVIRONMENT

    with pytest.raises(LegacyMigrationConflictError):
        await migrate_static_identities(
            document_path=path,
            directory=directory,
            tenant_id=_TENANT,
            environment_id=environment_id,
            actor_user_id=_ACTOR_USER_ID,
            actor=_ACTOR,
        )

    assert _facts(memory_state) == {
        "accounts": 0,
        "roles": 0,
        "bindings": 0,
        "audits": 0,
    }


@pytest.mark.parametrize(
    ("name", "make"),
    [
        (
            "actor",
            lambda tmp: _document(tmp, _entry("subject-long", "a" * 257, "operator")),
        ),
        (
            "subject_ref",
            lambda tmp: _document(
                tmp, _entry("o" * (CONTROLLED_PII_MAX_LENGTH + 1), "bob", "operator")
            ),
        ),
        (
            "environment_id",
            lambda tmp: _document(
                tmp, _entry("subject-alice", "alice", "operator"), environment_id="e" * 65
            ),
        ),
    ],
)
async def test_the_bound_failure_happens_before_any_apply(
    tmp_path: Path, directory, name: str, make
) -> None:
    """上一条红在**组装期**，不是"写到一半"。

    三个字段各走一遍：entry、command 与 context 的契约拒绝必须都发生在
    ``apply()`` 之前，否则"整批零写入"靠的就只是事务回滚，而不是根本没开始写。
    """

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
    path = make(tmp_path)
    environment_id = "e" * 65 if name == "environment_id" else _ENVIRONMENT

    with pytest.raises(LegacyMigrationConflictError):
        await migrate_static_identities(
            document_path=path,
            directory=counting,
            tenant_id=_TENANT,
            environment_id=environment_id,
            actor_user_id=_ACTOR_USER_ID,
            actor=_ACTOR,
        )

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


async def _seed_a_fully_migrated_alice(directory, *, role=ProductRole.OPERATOR) -> str:
    """把 alice 建成"已经完整迁移过"的样子：五项全部正确。

    三条反例各自只推翻其中**一项**，其余全部留在正确值上；否则它可能是被别的
    判据挡住的，删掉目标那一项检查它照样绿。
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
            role=role,
        ),
        "seed-migrated-1",
    )
    await _seed(
        directory,
        BindExternalIdentityCommand(
            user_id=user_id,
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            subject_ref="subject-alice",
        ),
        "seed-migrated-2",
    )
    return user_id


async def test_a_fully_migrated_entry_is_the_positive_control(
    tmp_path: Path, directory, memory_state
) -> None:
    """正常对照：五项全部精确匹配时判 `skipped`，一行都不写。

    没有这一条，下面三条反例证明不了"是那一项让它变成冲突的"——一个永远抛冲突
    的实现也能让三条反例全绿。
    """
    await _seed_a_fully_migrated_alice(directory)
    before = _facts(memory_state)
    path = _document(tmp_path, _entry("subject-alice", "alice", "operator"))

    report = await _migrate(path, directory)

    assert report.created == 0 and report.skipped == 1
    assert _facts(memory_state) == before


async def test_an_actor_that_no_longer_matches_is_a_conflict(
    tmp_path: Path, directory, memory_state
) -> None:
    """不变量 2 的"actor 不符"一格：这个 `user_id` 背后是**另一个人**。

    这不是硬摆出来的状态：管理员用 `CreateUserCommand` 自选 `user_id` 时完全可能
    撞上迁移派生出的那一个。除 actor 外的四项全部按正确值建好。
    """
    user_id = legacy_user_id(actor="alice")
    await _seed(
        directory,
        CreateUserCommand(
            user_id=user_id,
            actor="somebody-else",
            display_name="somebody else",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.OPERATOR,
        ),
        "seed-actor-drift-1",
    )
    await _seed(
        directory,
        BindExternalIdentityCommand(
            user_id=user_id,
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            subject_ref="subject-alice",
        ),
        "seed-actor-drift-2",
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


async def test_a_disabled_account_is_a_conflict_not_a_skip(
    tmp_path: Path, directory, memory_state
) -> None:
    """不变量 2 的"状态不符"一格：被停用的账号不算迁移完成。

    这一格**不由迁移层自己判**：`load_account` 对停用账号返回 `None`（停用即刻
    生效），于是该条目落进批次、撞上账号唯一约束，整批零写入。因此这条用例钉的是
    端到端性质，而它真正依赖的保护在 `load_account` 里——把那里的状态过滤去掉，
    这条就会变红。
    """
    user_id = await _seed_a_fully_migrated_alice(directory)
    await _seed(
        directory,
        SetUserStatusCommand(
            user_id=user_id,
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            status=UserStatus.DISABLED,
        ),
        "seed-disabled",
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


async def test_a_subject_bound_to_another_account_is_a_conflict(
    tmp_path: Path, directory, memory_state
) -> None:
    """不变量 2 的"绑定指向别人"一格。

    它与"绑定缺失"是**两个**判据：那一格 `resolve_by_subject` 返回 `None`，这一格
    返回的是一个**别人的**账号。只钉前者时，去掉"必须解析到同一个 user_id"这半句
    检查仍然全绿。
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
        "seed-bound-elsewhere-1",
    )
    await _seed(
        directory,
        CreateUserCommand(
            user_id="impostor",
            actor="impostor",
            display_name="impostor",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.USER,
        ),
        "seed-bound-elsewhere-2",
    )
    await _seed(
        directory,
        BindExternalIdentityCommand(
            user_id="impostor",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            subject_ref="subject-alice",
        ),
        "seed-bound-elsewhere-3",
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


async def test_a_subject_bound_to_a_disabled_account_is_a_closed_conflict(
    tmp_path: Path, directory, memory_state
) -> None:
    """目录的“已绑定但不可用”异常不能泄出迁移模块。

    派生账号的 actor、状态和角色全部匹配；同一个飞书主体只绑定到另一个已停用
    账号。这样唯一被推翻的事实就是绑定目标可用性，而不是账号缺失、角色不符或
    绑定缺失。迁移必须把目录层异常收敛为自己的闭集冲突，并保持整批零写入。
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
        "seed-disabled-binding-1",
    )
    await _seed(
        directory,
        CreateUserCommand(
            user_id="disabled-owner",
            actor="disabled-owner",
            display_name="disabled owner",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            role=ProductRole.USER,
        ),
        "seed-disabled-binding-2",
    )
    await _seed(
        directory,
        BindExternalIdentityCommand(
            user_id="disabled-owner",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            subject_ref="subject-alice",
        ),
        "seed-disabled-binding-3",
    )
    await _seed(
        directory,
        SetUserStatusCommand(
            user_id="disabled-owner",
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT,
            status=UserStatus.DISABLED,
        ),
        "seed-disabled-binding-4",
    )
    before = _facts(memory_state)
    path = _document(tmp_path, _entry("subject-alice", "alice", "operator"))

    with pytest.raises(LegacyMigrationConflictError):
        await _migrate(path, directory)

    assert _facts(memory_state) == before

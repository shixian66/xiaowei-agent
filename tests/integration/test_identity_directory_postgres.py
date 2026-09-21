"""身份目录与 Admin 审计在**真实 PostgreSQL** 上的同事务实证。

共享套件在这里跑第二遍。集成侧必须用**同名 fixture** 覆盖根 conftest 的内存实现
——只写 ``bind(...)`` 而不定义同名 fixture，本文件会安静地继续跑内存实现，
"PostgreSQL 同事务证据"就是假绿。``test_the_bound_fixtures_are_the_postgres_implementations``
就是为了让这件事没法安静发生。
"""

import datetime as dt

import pytest
import sqlalchemy as sa
from tests.suites.identity_directory import (
    IDENTITY_DIRECTORY_CASES,
    TENANT,
    bind,
    context,
    create_user,
)

from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
)
from xiaowei_agent.contracts.identity import (
    LOCAL_ADMIN_ENVIRONMENT_ID,
    LOCAL_ADMIN_TENANT_ID,
    LOCAL_ADMIN_USER_ID,
)
from xiaowei_agent.persistence import postgres
from xiaowei_agent.persistence.errors import PersistenceIntegrityError
from xiaowei_agent.persistence.identity import AdminAuditUnwritableError
from xiaowei_agent.persistence.local_admin import (
    LOCAL_ADMIN_SINGLETON_ID,
    ChangePasswordCommand,
    PostgresLocalAdminStore,
)
from xiaowei_agent.persistence.postgres import (
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
)
from xiaowei_agent.persistence.schema import (
    ADMIN_AUDIT_EVENTS,
    LOCAL_ADMINS,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
)

_LEGACY_PASSWORD_HASH = "legacy" + "-argon2id-digest"
_ROTATED_PASSWORD_HASH = "rotated" + "-argon2id-digest"
_W1A_TABLES = (USER_ACCOUNTS, USER_ROLE_ASSIGNMENTS, ADMIN_AUDIT_EVENTS)


@pytest.fixture
def directory(clean_database, clock):
    return PostgresUserDirectoryStore(engine=clean_database, clock=clock)


@pytest.fixture
def audit(clean_database, clock):
    return PostgresAdminAuditStore(engine=clean_database, clock=clock)


@pytest.fixture
def admins(clean_database, clock):
    return PostgresLocalAdminStore(engine=clean_database, clock=clock)


@pytest.fixture
def admin_probe(clean_database):
    """PostgreSQL 侧的旧凭据探针；直接摆列，绕过 ``apply()``。

    它制造的正是 rev_0014 之前的版本写下的那种行：有凭据、没有链接。
    """

    class _Probe:
        async def seed_legacy_credential(
            self, *, password_hash: str, user_id: str | None = None
        ) -> bool:
            """返回值是"悬空链接到底造出来没有"。

            ``fk_local_admins_user_id`` 是 ``RESTRICT``，指向不存在账号的那一行
            数据库直接拒收——所以这里**真的去插一次**并确认被拒，而不是凭注释
            断言它不可能。凭空返回 ``False`` 就是一条空检查。
            """
            try:
                async with clean_database.begin() as connection:
                    await connection.execute(
                        sa.insert(LOCAL_ADMINS).values(
                            id=LOCAL_ADMIN_SINGLETON_ID,
                            password_hash=password_hash,
                            must_change_password=True,
                            updated_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
                            user_id=user_id,
                        )
                    )
            except sa.exc.IntegrityError:
                assert user_id is not None
                return False
            return user_id is not None

        async def linked_user_id(self) -> str | None:
            async with clean_database.connect() as connection:
                value = await connection.scalar(
                    sa.select(LOCAL_ADMINS.c.user_id).where(
                        LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
                    )
                )
            return None if value is None else str(value)

        async def stored_password_hash(self) -> str:
            async with clean_database.connect() as connection:
                value = await connection.scalar(
                    sa.select(LOCAL_ADMINS.c.password_hash).where(
                        LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
                    )
                )
            return str(value)

    return _Probe()


@pytest.fixture
def broken_audit_derivation(monkeypatch):
    """把 ``postgres`` 模块里的 ``derive_audit`` 换成与领域无关的爆炸。

    改的是 ``postgres`` 上的那个名字，不是 ``identity`` 上的：两个实现各自
    ``from ... import derive_audit``，改源模块的属性对已经绑好的名字没有作用。
    """

    def activate() -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit derivation exploded")

        monkeypatch.setattr(postgres, "derive_audit", boom)

    return activate


bind(globals(), IDENTITY_DIRECTORY_CASES)


async def _count(engine, table) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0
        )


def test_the_bound_fixtures_are_the_postgres_implementations(
    directory, audit, admins
) -> None:
    """防止整份"同事务证据"假绿。

    少写一个同名 fixture，上面那 24 条就会继续跑内存实现且全绿——而本文件存在的
    唯一理由是"在真库上再证一遍"。
    """
    assert isinstance(directory, PostgresUserDirectoryStore)
    assert isinstance(audit, PostgresAdminAuditStore)
    assert isinstance(admins, PostgresLocalAdminStore)


async def test_rolled_back_change_leaves_no_row_in_either_table(
    directory, clean_database
) -> None:
    """真库上的同事务回滚：账号与审计**两张表**都必须空。

    只查一张表时，"授权写了、审计没写"与"两个都没写"分辨不出来——而前者正是本
    阶段要排除的那个半状态。
    """
    await directory.apply(command=create_user("alice"), context=context("op-dup"))

    with pytest.raises(AdminAuditUnwritableError):
        await directory.apply(
            command=create_user("bob", actor="bob@example.com"),
            context=context("op-dup"),
        )

    assert await _count(clean_database, USER_ACCOUNTS) == 1
    assert await _count(clean_database, ADMIN_AUDIT_EVENTS) == 1


async def test_an_unknown_constraint_is_not_dressed_up_as_a_business_conflict(
    directory, clean_database, monkeypatch
) -> None:
    """CHECK 违规必须收敛成 ``PersistenceIntegrityError``，不是业务冲突。

    ``ON CONFLICT DO NOTHING`` 只吞唯一/主键/偏唯一三种冲突；CHECK、外键、NOT NULL
    照抛。伪造一条契约层拒绝、数据库层也拒绝的事件（成功却对一个 ``role_revoked``
    声称角色 effect），用 ``model_construct`` 绕过契约校验直接送到数据库——如果它
    被当成 ``UserDirectoryConflictError`` 抛出来，就说明有人在拿异常分类冲突。
    """
    monkeypatch.setattr(
        postgres,
        "admin_audit_event_to_row",
        lambda event: {
            "event_id": "f" * 64,
            "operation_id": "op-forged",
            "tenant_id": TENANT,
            "environment_id": "env-a",
            "actor_user_id": "operator-1",
            "actor": "carol@example.com",
            "auth_source": IdentitySource.LOCAL_ADMIN.value,
            "action": AdminAuditAction.ROLE_REVOKED.value,
            "target_kind": AdminAuditTargetKind.USER.value,
            "target_ref_digest": "a" * 64,
            "outcome": AdminAuditOutcome.SUCCEEDED.value,
            "reason_code": None,
            # 这一格就是 CHECK 要拒的东西：撤销不该声称任何角色。
            "effect_role": ProductRole.ADMIN.value,
            "effect_status": None,
            "created_at": dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        },
    )

    with pytest.raises(PersistenceIntegrityError):
        await directory.apply(command=create_user("alice"), context=context("op-ck"))

    for table in _W1A_TABLES:
        assert await _count(clean_database, table) == 0


async def test_seeding_the_local_admin_writes_its_audit_event(
    admins, clean_database
) -> None:
    """``seed_if_absent`` 真的走了 ``apply()``——否则这张表里不会有任何行。"""
    assert await admins.seed_if_absent(password_hash=_LEGACY_PASSWORD_HASH) is True

    async with clean_database.connect() as connection:
        rows = (
            (await connection.execute(sa.select(ADMIN_AUDIT_EVENTS))).mappings().all()
        )
        linked = await connection.scalar(
            sa.select(LOCAL_ADMINS.c.user_id).where(
                LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
            )
        )
    assert len(rows) == 1
    assert rows[0]["action"] == AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED.value
    assert rows[0]["effect_role"] == ProductRole.ADMIN.value
    assert linked == LOCAL_ADMIN_USER_ID
    assert await admins.seed_if_absent(password_hash=_LEGACY_PASSWORD_HASH) is False


async def test_an_already_seeded_database_gets_its_directory_backfilled_on_upgrade(
    admins, clean_database, alembic_runners
) -> None:
    """四态表第二行的真库版：降级—插入旧行—升级—装配。

    ``local_stack.py:1032`` 在**每次**装配 Web 时都调用 ``seed_if_absent``，任何跑过
    RI5 的数据库早就有这一行。按"已有行即 no-op"实现，升级之后 ``user_id`` 永远是
    ``NULL``，而全新安装的测试全绿——这是一条只在真实升级路径上才踩得到的裂缝。

    本用例主动降级，因此必须 ``try/finally`` 无条件升回 ``head`` 并断言恢复；
    ``migrated_engine`` 是 session 级、只升一次，``clean_database`` 只 ``TRUNCATE``
    不重建 schema，漏了这一步后续用例会在 ``rev_0013`` 上运行。
    """
    run_upgrade, run_downgrade = alembic_runners
    try:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0013_clarification_parent")
            await connection.execute(
                sa.text(
                    "INSERT INTO local_admins (id, password_hash,"
                    " must_change_password, updated_at)"
                    " VALUES (:id, :hash, TRUE, NOW())"
                ),
                {"id": LOCAL_ADMIN_SINGLETON_ID, "hash": _LEGACY_PASSWORD_HASH},
            )
            await connection.run_sync(run_upgrade)

        async with clean_database.connect() as connection:
            assert (
                await connection.scalar(
                    sa.select(LOCAL_ADMINS.c.user_id).where(
                        LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
                    )
                )
                is None
            )

        assert await admins.seed_if_absent(password_hash="ignored" + "-hash") is True

        async with clean_database.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(LOCAL_ADMINS).where(
                            LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
                        )
                    )
                )
                .mappings()
                .first()
            )
            role = await connection.scalar(
                sa.select(USER_ROLE_ASSIGNMENTS.c.role).where(
                    USER_ROLE_ASSIGNMENTS.c.user_id == LOCAL_ADMIN_USER_ID,
                    USER_ROLE_ASSIGNMENTS.c.tenant_id == LOCAL_ADMIN_TENANT_ID,
                    USER_ROLE_ASSIGNMENTS.c.environment_id
                    == LOCAL_ADMIN_ENVIRONMENT_ID,
                )
            )
        assert row is not None
        assert row["user_id"] == LOCAL_ADMIN_USER_ID
        # 原口令一个字节都不动：backfill 拿初始口令覆盖回去等于静默的凭据回滚。
        assert row["password_hash"] == _LEGACY_PASSWORD_HASH
        assert role == ProductRole.ADMIN.value
    finally:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_upgrade)
        async with clean_database.connect() as connection:
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            columns = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'local_admins' AND column_name = 'user_id'"
                )
            )
        assert revision == "0014_identity_admin_audit"
        assert columns == 1


async def test_changing_the_password_keeps_the_directory_link(
    admins, clean_database
) -> None:
    """链接必须同时出现在三处：数据库列、改密**返回值**、下一次 ``get()``。

    只断言数据库列时，``get()`` 与返回值可以双双返回 ``None`` 而全绿——而调用方
    读到的正是后两者。
    """
    await admins.seed_if_absent(password_hash=_LEGACY_PASSWORD_HASH)

    rotated = await admins.change_password_and_rotate_session(
        command=ChangePasswordCommand(
            password_hash=_ROTATED_PASSWORD_HASH,
            new_session_digest="b" * 64,
            public_origin_digest="a1" * 32,
            session_ttl_seconds=3600,
        )
    )

    async with clean_database.connect() as connection:
        stored = await connection.scalar(
            sa.select(LOCAL_ADMINS.c.user_id).where(
                LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
            )
        )
    assert stored == LOCAL_ADMIN_USER_ID
    assert rotated.user_id == LOCAL_ADMIN_USER_ID
    assert (await admins.get()).user_id == LOCAL_ADMIN_USER_ID

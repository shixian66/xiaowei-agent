"""身份目录与 Admin 审计在**真实 PostgreSQL** 上的同事务实证。

共享套件在这里跑第二遍。集成侧必须用**同名 fixture** 覆盖根 conftest 的内存实现
——只写 ``bind(...)`` 而不定义同名 fixture，本文件会安静地继续跑内存实现，
"PostgreSQL 同事务证据"就是假绿。``test_the_bound_fixtures_are_the_postgres_implementations``
就是为了让这件事没法安静发生。
"""

import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
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
    BindExternalIdentityCommand,
    UnbindExternalIdentityCommand,
)
from xiaowei_agent.persistence import postgres
from xiaowei_agent.persistence.errors import PersistenceIntegrityError
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryNotFoundError,
)
from xiaowei_agent.persistence.local_admin import (
    LOCAL_ADMIN_SINGLETON_ID,
    ChangePasswordCommand,
    PostgresLocalAdminStore,
)
from xiaowei_agent.persistence.postgres import (
    BOOTSTRAP_LOCAL_ADMIN_LOCK_KEY,
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
)
from xiaowei_agent.persistence.schema import (
    ADMIN_AUDIT_EVENTS,
    EXTERNAL_IDENTITIES,
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
def directory_probe(clean_database):
    """PostgreSQL 侧的逐类事实读；键的形状与内存侧**逐列相同**。

    形状不一致时，同一条共享用例会在两个实现上比较两种东西，而那正是共享套件要
    排除的分叉。
    """

    class _Probe:
        async def facts(self) -> dict[str, frozenset]:
            async with clean_database.connect() as connection:
                accounts = await connection.execute(
                    sa.select(USER_ACCOUNTS.c.user_id)
                )
                roles = await connection.execute(
                    sa.select(
                        USER_ROLE_ASSIGNMENTS.c.user_id,
                        USER_ROLE_ASSIGNMENTS.c.tenant_id,
                        USER_ROLE_ASSIGNMENTS.c.environment_id,
                    )
                )
                bindings = await connection.execute(
                    sa.select(
                        EXTERNAL_IDENTITIES.c.provider,
                        EXTERNAL_IDENTITIES.c.tenant_id,
                        EXTERNAL_IDENTITIES.c.environment_id,
                        EXTERNAL_IDENTITIES.c.subject_ref_digest,
                    )
                )
                audits = await connection.execute(
                    sa.select(ADMIN_AUDIT_EVENTS.c.event_id)
                )
                return {
                    "accounts": frozenset(accounts.scalars()),
                    "roles": frozenset(tuple(row) for row in roles),
                    "bindings": frozenset(tuple(row) for row in bindings),
                    "audits": frozenset(audits.scalars()),
                }

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


async def _wait_until_a_backend_blocks(engine, *, timeout: float = 5.0) -> bool:
    """轮询到确实有后端在等锁为止。

    不用固定 ``sleep``：固定等待要么太短（还没进到那条语句，窗口根本没打开，用例
    会在**有缺陷的代码上也通过**），要么太长（白白拖慢每一次运行）。这里等的是一个
    可观测的事实——"有人被锁挡住了"。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with engine.connect() as connection:
            blocked = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE state = 'active' AND wait_event_type = 'Lock'"
                )
            )
        if blocked:
            return True
        await asyncio.sleep(0.02)
    return False


async def _wait_until_a_backend_waits_for_an_advisory_lock(
    engine, *, timeout: float = 5.0
) -> bool:
    """轮询到确实有后端在等一把 **advisory** 锁为止。

    比"等任意一把锁"更窄：行锁、表锁都会让上一个 helper 返回真，而这里要证明的
    恰恰是 bootstrap 走了 advisory 这条路。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with engine.connect() as connection:
            blocked = await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE state = 'active' AND wait_event_type = 'Lock'"
                    " AND wait_event = 'advisory'"
                )
            )
        if blocked:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_unbinding_a_subject_another_transaction_already_removed_is_not_found(
    directory, clean_database, postgres_dsn
) -> None:
    """**并发解绑不得写出一条虚假的成功审计。**

    复现是确定性的，不靠两个客户端抢跑：另一个事务先删掉绑定但**不提交**，我们的
    解绑因此在行锁上阻塞——阻塞本身就证明它已经越过了前置判定；对方提交之后，我们
    那条 ``DELETE`` 实际影响 0 行。

    旧写法在这一刻返回成功并记一条 ``external_identity_unbound`` 的 ``SUCCEEDED``：
    一次没有发生的授权改变，在审计里留下了发生过的证据。而审计是事后唯一的依据。
    """
    await directory.apply(command=create_user("alice"), context=context("op-1"))
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id="env-a",
            subject_ref="ou_alice",
        ),
        context=context("op-2"),
    )

    rival = create_async_engine(postgres_dsn, poolclass=sa.pool.NullPool)
    try:
        connection = await rival.connect()
        transaction = await connection.begin()
        await connection.execute(sa.delete(EXTERNAL_IDENTITIES))

        async def unbind() -> str:
            try:
                await directory.apply(
                    command=UnbindExternalIdentityCommand(
                        user_id="alice", tenant_id=TENANT, environment_id="env-a"
                    ),
                    context=context("op-unbind"),
                )
                return "success"
            except UserDirectoryNotFoundError:
                return "not-found"

        task = asyncio.create_task(unbind())
        assert await _wait_until_a_backend_blocks(clean_database), (
            "解绑没有在行锁上阻塞——窗口没打开，这条用例证明不了任何事"
        )
        await transaction.commit()
        await connection.close()
        outcome = await task
    finally:
        await rival.dispose()

    assert outcome == "not-found"
    async with clean_database.connect() as connection:
        unbind_audits = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(ADMIN_AUDIT_EVENTS)
            .where(
                ADMIN_AUDIT_EVENTS.c.action
                == AdminAuditAction.EXTERNAL_IDENTITY_UNBOUND.value
            )
        )
    assert unbind_audits == 0, "没有发生的解绑不得留下审计"


async def test_a_second_bootstrap_waits_for_the_advisory_lock(
    admins, clean_database, postgres_dsn
) -> None:
    """**bootstrap 必须在 advisory lock 上排队。**

    两个 Web 进程同时装配会同时调用 ``seed_if_absent``。没有这把锁时，两边都读到
    "还没有凭据行"、都去建账号，第二个撞 ``user_accounts`` 主键——一次正常的并发
    启动变成一次启动失败。

    这里不靠两个客户端抢跑来碰运气：外面先把**同一把锁**攥在一个未提交的事务里，
    于是装配必然排队；"它确实在等一把 advisory lock"是一个可观测的事实，而不是一段
    推断。锁一旦被换成一条无效语句，装配会立刻跑完，下面那条断言随即变红。
    """
    holder = create_async_engine(postgres_dsn, poolclass=sa.pool.NullPool)
    try:
        connection = await holder.connect()
        transaction = await connection.begin()
        await connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(
                lock_key=BOOTSTRAP_LOCAL_ADMIN_LOCK_KEY
            )
        )

        task = asyncio.create_task(
            admins.seed_if_absent(password_hash=_LEGACY_PASSWORD_HASH)
        )
        waited = await _wait_until_a_backend_waits_for_an_advisory_lock(clean_database)
        assert waited, "bootstrap 没有在 advisory lock 上排队"
        assert not task.done()

        await transaction.commit()
        await connection.close()
        assert await task is True
    finally:
        await holder.dispose()

    async with clean_database.connect() as connection:
        linked = await connection.scalar(
            sa.select(LOCAL_ADMINS.c.user_id).where(
                LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
            )
        )
    assert linked == LOCAL_ADMIN_USER_ID


async def test_simultaneous_assembly_writes_exactly_one_local_admin(
    clean_database, postgres_dsn, clock
) -> None:
    """八个进程同时装配：一次写入、七次幂等，**没有一次抛异常**。

    这是上一条的端到端对照。它证明的不是锁的机制，而是调用方看到的结果：启动不会
    因为"另一个进程刚好也在启动"而失败，四类事实也不会各写出两份。
    """
    engines = [
        create_async_engine(postgres_dsn, poolclass=sa.pool.NullPool) for _ in range(8)
    ]
    try:
        stores = [
            PostgresLocalAdminStore(engine=engine, clock=clock) for engine in engines
        ]
        barrier = asyncio.Barrier(len(stores))

        async def seed(store) -> bool:
            await barrier.wait()
            return await store.seed_if_absent(password_hash=_LEGACY_PASSWORD_HASH)

        results = await asyncio.gather(*(seed(store) for store in stores))
    finally:
        for engine in engines:
            await engine.dispose()

    assert sum(results) == 1, results
    async with clean_database.connect() as connection:
        credentials = await connection.scalar(
            sa.select(sa.func.count()).select_from(LOCAL_ADMINS)
        )
        accounts = await connection.scalar(
            sa.select(sa.func.count()).select_from(USER_ACCOUNTS)
        )
        roles = await connection.scalar(
            sa.select(sa.func.count()).select_from(USER_ROLE_ASSIGNMENTS)
        )
        audits = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(ADMIN_AUDIT_EVENTS)
            .where(
                ADMIN_AUDIT_EVENTS.c.action
                == AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED.value
            )
        )
    assert (credentials, accounts, roles, audits) == (1, 1, 1, 1)

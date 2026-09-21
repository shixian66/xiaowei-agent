"""本地管理员的存储契约与 PostgreSQL 实现。

改密路径**必须**由这里的单个方法完成：口令更新、清除强制改密标记、撤销全部既有
本地管理员 session、签发新 session 四件事跨了 ``local_admins`` 与 ``web_sessions``
两张表，分到两个 Store 就会出现"口令已改但旧 session 还活着"的中间态。
"""

import datetime as _dt
from typing import Protocol

import sqlalchemy as sa
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.contracts import (
    Contract,
    IdentitySource,
    SecretHash,
    Sha256Hex,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.identity import (
    LOCAL_ADMIN_ACTOR,
    LOCAL_ADMIN_DISPLAY_NAME,
    LOCAL_ADMIN_ENVIRONMENT_ID,
    LOCAL_ADMIN_TENANT_ID,
    LOCAL_ADMIN_USER_ID,
    BootstrapLocalAdminCommand,
)
from xiaowei_agent.persistence.identity import (
    BOOTSTRAP_ACTOR_USER_ID,
    BOOTSTRAP_OPERATION_ID,
)
from xiaowei_agent.persistence.schema import LOCAL_ADMINS, WEB_SESSIONS
from xiaowei_agent.persistence.store import Clock

LOCAL_ADMIN_SINGLETON_ID = 1
"""``local_admins`` 唯一那一行的主键。

公开而不是私有：测试与迁移用例都要指这一行，留成私有名就会在别处再抄一遍字面量，
而抄出来的那一份不会跟着这里改。
"""

LOCAL_ADMIN_SUBJECT_REF = "local-admin"
"""本地管理员的固定主体引用；不是用户输入，也不参与身份目录解析。"""


class LocalAdminStoreError(RuntimeError):
    """不把口令、哈希或 cookie 写入异常文本的存储错误。"""


class LocalAdminNotFoundError(LocalAdminStoreError, LookupError):
    """本地管理员尚未 seed。"""

    def __init__(self) -> None:
        super().__init__("local admin not found")


class LocalAdminRecord(Contract):
    """本地管理员的持久化事实；只有哈希，没有明文。"""

    password_hash: SecretHash = Field(exclude=True, repr=False)
    must_change_password: bool
    user_id: StrictStr | None = None
    """指向身份目录里那个账号；``None`` 表示这行凭据还没有被链接。

    默认 ``None`` 不惊动既有调用方，但也让**每一个漏改的构造点安静地返回
    ``None``**——而 ``None`` 恰好是四态表里的 backfill 那一行。漏回填会让"数据库里
    已经链接好了"看起来像"还没升级"，于是下一次装配会去补写一份已经存在的授权。
    因此四个构造点必须逐个回填，``tests/security/test_identity_write_path.py``
    连同 ``test_changing_the_password_keeps_the_directory_link`` 一起钉住它们。

    它的**写入口只有** ``UserDirectoryStore.apply()``：这一列是授权事实，不是凭据。
    """


class ChangePasswordCommand(Contract):
    """一次改密所需的全部输入。

    新 session 的 digest 由调用方先生成后传入：store 不产生随机值，
    所有随机来源集中在认证服务一处。
    """

    password_hash: SecretHash = Field(exclude=True, repr=False)
    new_session_digest: Sha256Hex
    public_origin_digest: Sha256Hex
    session_ttl_seconds: StrictInt = Field(gt=0, le=86_400)


def bootstrap_operation_context() -> AdminOperationContext:
    """bootstrap 的操作上下文。

    操作者是保留的 :data:`BOOTSTRAP_ACTOR_USER_ID`，不是某个人——bootstrap 发生时
    目录里还没有任何 Admin 账号。它**不是**"这次不写审计"的例外。
    """
    return AdminOperationContext(
        operation_id=BOOTSTRAP_OPERATION_ID,
        actor_user_id=BOOTSTRAP_ACTOR_USER_ID,
        actor=BOOTSTRAP_ACTOR_USER_ID,
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


def bootstrap_local_admin_command(*, password_hash: str) -> BootstrapLocalAdminCommand:
    """两个实现共用同一条 bootstrap 命令，省得各拼一份常量。"""
    return BootstrapLocalAdminCommand(
        user_id=LOCAL_ADMIN_USER_ID,
        actor=LOCAL_ADMIN_ACTOR,
        display_name=LOCAL_ADMIN_DISPLAY_NAME,
        tenant_id=LOCAL_ADMIN_TENANT_ID,
        environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
        password_hash=password_hash,
    )


class LocalAdminStore(Protocol):
    """本地管理员的读写边界；同时是唯一触及 ``local_admins`` 的写入口。"""

    async def seed_if_absent(self, *, password_hash: str) -> bool:
        """不存在时写入初始口令并置强制改密；已存在则原样不动，返回是否写入。

        返回值是"本次调用有没有写下本地管理员事实"。链接完整时是幂等 no-op，
        返回 ``False``；凭据行已在但还没链接到目录账号时（rev_0014 之前的库）
        会补齐目录侧并返回 ``True``，此时**原口令一个字节都不动**。
        """

    async def get(self) -> LocalAdminRecord:
        """读取唯一一行；尚未 seed 时抛 :class:`LocalAdminNotFoundError`。"""

    async def change_password_and_rotate_session(
        self, *, command: ChangePasswordCommand
    ) -> LocalAdminRecord:
        """**单个事务**内改密、清标记、撤销全部旧 session 并签发新 session。"""


class PostgresLocalAdminStore:
    """``LocalAdminStore`` 的 PostgreSQL 实现。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def seed_if_absent(self, *, password_hash: str) -> bool:
        """委托给 ``apply()``，不再自己写这一行。

        ``local_admins.user_id`` 是授权事实，授权事实只有一条写路径；而凭据行与
        目录账号必须在**同一个事务**里出现，否则失败会留下一行无人链接的凭据。
        """
        from xiaowei_agent.persistence.postgres import PostgresUserDirectoryStore

        directory = PostgresUserDirectoryStore(engine=self._engine, clock=self._clock)
        events = await directory.apply(
            command=bootstrap_local_admin_command(password_hash=password_hash),
            context=bootstrap_operation_context(),
        )
        return bool(events)

    async def get(self) -> LocalAdminRecord:
        async with self._engine.connect() as connection:
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
        if row is None:
            raise LocalAdminNotFoundError
        return LocalAdminRecord(
            password_hash=row["password_hash"],
            must_change_password=row["must_change_password"],
            # ``SELECT`` 本来就是整表，新列自动在 ``row`` 里——正因为"自动在里面"，
            # 漏掉这一行不会有任何报错，只会让链接在读回时静默消失。
            user_id=row["user_id"],
        )

    async def change_password_and_rotate_session(
        self, *, command: ChangePasswordCommand
    ) -> LocalAdminRecord:
        now = self._clock()
        async with self._engine.begin() as connection:
            updated = (
                (
                    await connection.execute(
                        sa.update(LOCAL_ADMINS)
                        .where(LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID)
                        .values(
                            password_hash=command.password_hash,
                            must_change_password=False,
                            updated_at=now,
                        )
                        # ``user_id`` 一并 ``RETURNING``：链接**不能凭命令现搭**，
                        # ``ChangePasswordCommand`` 里根本没有这个字段。少了它，
                        # 改密的返回值会把链接说成 ``None``，而数据库里它还在。
                        .returning(LOCAL_ADMINS.c.id, LOCAL_ADMINS.c.user_id)
                    )
                )
                .mappings()
                .first()
            )
            if updated is None:
                raise LocalAdminNotFoundError
            # 顺序不能颠倒：先撤销全部旧的，再插入新的，否则会把刚签发的那个
            # 一起撤掉。
            await connection.execute(
                sa.update(WEB_SESSIONS)
                .where(
                    WEB_SESSIONS.c.auth_source == IdentitySource.LOCAL_ADMIN.value,
                    WEB_SESSIONS.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            await connection.execute(
                sa.insert(WEB_SESSIONS).values(
                    session_digest=command.new_session_digest,
                    subject_ref=LOCAL_ADMIN_SUBJECT_REF,
                    issued_at=now,
                    expires_at=now
                    + _dt.timedelta(seconds=command.session_ttl_seconds),
                    revoked_at=None,
                    auth_source=IdentitySource.LOCAL_ADMIN.value,
                    public_origin_digest=command.public_origin_digest,
                )
            )
        return LocalAdminRecord(
            password_hash=command.password_hash,
            must_change_password=False,
            user_id=updated["user_id"],
        )


__all__ = [
    "LOCAL_ADMIN_SINGLETON_ID",
    "LOCAL_ADMIN_SUBJECT_REF",
    "ChangePasswordCommand",
    "LocalAdminNotFoundError",
    "LocalAdminRecord",
    "LocalAdminStore",
    "LocalAdminStoreError",
    "PostgresLocalAdminStore",
    "SecretHash",
    "bootstrap_local_admin_command",
    "bootstrap_operation_context",
]
"""``SecretHash`` 的定义已下沉到 ``contracts/base.py``（契约层不能反向依赖
``persistence``，而 ``contracts/identity.py`` 要按同一套约定标注口令哈希），这里只是
重新导出，不打断既有调用方。

**模块里只能有这一份 ``__all__``。** 上一版在模块中段另写了一份（因为没去找现有
的那份），而后赋值的这份静默覆盖了它：``SecretHash`` 属性在、却不在 ``__all__``
里，与注释声称的相反。当前仓库没有通配导入调用方，所以它没造成真实故障；
但"声明了却不生效"这个形状本身就是下一个缺陷的形状。
"""

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
)
from xiaowei_agent.persistence.schema import LOCAL_ADMINS, WEB_SESSIONS
from xiaowei_agent.persistence.store import Clock

_SINGLETON_ID = 1

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


class ChangePasswordCommand(Contract):
    """一次改密所需的全部输入。

    新 session 的 digest 由调用方先生成后传入：store 不产生随机值，
    所有随机来源集中在认证服务一处。
    """

    password_hash: SecretHash = Field(exclude=True, repr=False)
    new_session_digest: Sha256Hex
    public_origin_digest: Sha256Hex
    session_ttl_seconds: StrictInt = Field(gt=0, le=86_400)


class LocalAdminStore(Protocol):
    """本地管理员的读写边界；同时是唯一触及 ``local_admins`` 的写入口。"""

    async def seed_if_absent(self, *, password_hash: str) -> bool:
        """不存在时写入初始口令并置强制改密；已存在则原样不动，返回是否写入。"""

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
        now = self._clock()
        async with self._engine.begin() as connection:
            inserted = await connection.scalar(
                sa.dialects.postgresql.insert(LOCAL_ADMINS)
                .values(
                    id=_SINGLETON_ID,
                    password_hash=password_hash,
                    must_change_password=True,
                    updated_at=now,
                )
                .on_conflict_do_nothing()
                .returning(LOCAL_ADMINS.c.id)
            )
        return inserted is not None

    async def get(self) -> LocalAdminRecord:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(LOCAL_ADMINS).where(
                            LOCAL_ADMINS.c.id == _SINGLETON_ID
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
        )

    async def change_password_and_rotate_session(
        self, *, command: ChangePasswordCommand
    ) -> LocalAdminRecord:
        now = self._clock()
        async with self._engine.begin() as connection:
            updated = await connection.scalar(
                sa.update(LOCAL_ADMINS)
                .where(LOCAL_ADMINS.c.id == _SINGLETON_ID)
                .values(
                    password_hash=command.password_hash,
                    must_change_password=False,
                    updated_at=now,
                )
                .returning(LOCAL_ADMINS.c.id)
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
            password_hash=command.password_hash, must_change_password=False
        )


__all__ = [
    "LOCAL_ADMIN_SUBJECT_REF",
    "ChangePasswordCommand",
    "LocalAdminNotFoundError",
    "LocalAdminRecord",
    "LocalAdminStore",
    "LocalAdminStoreError",
    "PostgresLocalAdminStore",
    "SecretHash",
]
"""``SecretHash`` 的定义已下沉到 ``contracts/base.py``（契约层不能反向依赖
``persistence``，而 ``contracts/identity.py`` 要按同一套约定标注口令哈希），这里只是
重新导出，不打断既有调用方。

**模块里只能有这一份 ``__all__``。** 上一版在模块中段另写了一份（因为没去找现有
的那份），而后赋值的这份静默覆盖了它：``SecretHash`` 属性在、却不在 ``__all__``
里，与注释声称的相反。当前仓库没有通配导入调用方，所以它没造成真实故障；
但"声明了却不生效"这个形状本身就是下一个缺陷的形状。
"""

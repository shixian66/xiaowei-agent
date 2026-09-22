"""身份目录的持久化事实与写命令。

**本模块只描述"是什么"和"要做什么"，不描述"发生了什么"。** 后者是审计，由
``UserDirectoryStore`` 从命令派生（见 ``contracts/admin_audit.py``）。因此这里的
八个命令一律没有 ``action`` / ``target_kind`` / ``outcome`` / ``effect``，也没有
``created_at`` / ``created_by`` / ``event_id`` —— 能被调用方指定，就能被调用方写错，
而"校验它有没有写错"永远弱于"它根本没有机会写"。

**与规格 §6.1 的有意偏离**：:class:`UserAccount` 不带 ``tenant_id`` / ``environment_id``，
``actor`` 全局唯一；作用域只出现在 :class:`UserRoleAssignment` 上。规格要求"在同一
作用域内拒绝 actor 冲突"，全局唯一严格强于它。

五个本地管理员常量也住在这一层，因为 ``contracts`` 是**唯一一个 ``persistence`` 与
``interfaces`` 都能导入的层**（``tests/security/test_module_layering.py`` 禁止
``persistence -> interfaces``）。
"""

from typing import Annotated, Final, Literal, TypeAlias

from pydantic import Field

from xiaowei_agent.contracts.base import (
    CONTROLLED_PII_MAX_LENGTH,
    AwareDatetime,
    Contract,
    ControlledPii,
    SecretHash,
    StrictStr,
)
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole, UserStatus

BoundedId: TypeAlias = Annotated[StrictStr, Field(min_length=1, max_length=64)]
"""持久 ID 的统一上限。

由外部输入派生的 ID 必须先经摘要截断再落到这里，**不得**直接拼接 actor 这类没有
长度上限的外部字符串——拼接的话，一个长 actor 会在写库时才炸，而且是在批量中途。
"""

BoundedName: TypeAlias = Annotated[StrictStr, Field(min_length=1, max_length=128)]
"""纯显示字段的上限。这是唯一允许**截断**旧数据的一类字段：它不参与任何身份或
授权判定。"""

BoundedActor: TypeAlias = Annotated[StrictStr, Field(min_length=1, max_length=256)]
"""``actor`` 的上限，比 :data:`BoundedName` 宽。

``actor`` 是**身份**、不可截断（截断会把两个人合成一个），而它要接收的旧静态文档
对 actor 没有任何长度上限。256 之下完整迁移，之上整批零写入——见切片 C 的转换策略。
"""

ControlledPiiField: TypeAlias = Annotated[
    ControlledPii,
    Field(
        min_length=1,
        max_length=CONTROLLED_PII_MAX_LENGTH,
        exclude=True,
        repr=False,
    ),
]
"""受控 PII 字段的完整标注：有界 + 两条外泄通道都关。

``exclude=True`` 只作用于 ``model_dump()``，``repr=False`` 只作用于 ``repr()``；
两条是独立通道，少写一条不会有任何反馈。

上限**不在这里另写一个数**，而是引用
:data:`~xiaowei_agent.contracts.base.CONTROLLED_PII_MAX_LENGTH`：同一个 ``open_id``
的上限在飞书入口、OAuth 入口和目录各写一份时，最窄的那一份会变成一道静默的兼容墙。
"""

LOCAL_ADMIN_TENANT_ID: Final[str] = "dev-local"
LOCAL_ADMIN_ENVIRONMENT_ID: Final[str] = "dev"
LOCAL_ADMIN_ACTOR: Final[str] = "admin"
LOCAL_ADMIN_USER_ID: Final[str] = "local-admin"
LOCAL_ADMIN_DISPLAY_NAME: Final[str] = "Local Admin"
"""本地管理员的固定标识。

前三个的值取自既有的 ``LOCAL_ADMIN_PRINCIPAL``；搬到契约层之后
``interfaces/local_admin_auth.py`` 改为导入并删掉自己那三个字面量。留着就是两份
真源，而两份真源迟早会在某次改租户名时只改一边。
"""


class UserAccount(Contract):
    """目录里的一个账号。``actor`` 全局唯一，作用域不在这里。"""

    user_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    status: UserStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class UserRoleAssignment(Contract):
    """某个账号在某个作用域里的角色。作用域住在这里，不住在账号上。"""

    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    role: ProductRole
    created_by: BoundedId
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ExternalIdentity(Contract):
    """外部主体到账号的绑定。

    ``provider`` 是 ``Literal`` 而不是整个 :class:`IdentitySource`：W1a 只有飞书一个
    外部来源，而本地管理员根本没有 subject——留成开放枚举就等于允许写进一行
    "本地管理员的外部身份"。
    """

    user_id: BoundedId
    provider: Literal[IdentitySource.FEISHU]
    tenant_id: BoundedId
    environment_id: BoundedId
    subject_ref: ControlledPiiField
    created_at: AwareDatetime
    last_seen_at: AwareDatetime


class DirectoryPrincipalFacts(Contract):
    """一次解析返回的全部授权事实：账号 + 该作用域的角色。"""

    account: UserAccount
    assignment: UserRoleAssignment


class LegacyIdentityMigrationEntry(Contract):
    """一条待迁移的旧身份，字段**已经是派生完的结果**。

    角色已按规格 §6.6 映射、``user_id`` 已摘要截断。它和切片 C 的
    ``interfaces.feishu_identity.LegacyIdentityEntry`` 是**两个类型，不要合并**：
    后者带的是原始 labels（``dba`` / ``oncall`` 这些外部标签）。合并成一个，要么让
    契约层去认识那些外部标签，要么让 ``persistence`` 反向依赖 ``interfaces``，而
    分层测试直接禁止后者。
    """

    user_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    role: ProductRole
    subject_ref: ControlledPiiField


_MAX_MIGRATION_ENTRIES: Final[int] = 10_000
"""一次迁移的条目上限。

有上限是因为整批走**一个**事务：没有上限时，一份异常大的旧文档会把一次迁移变成
一个长事务，而长事务在真实库上会拖住 autovacuum 并放大锁等待。
"""


class CreateUserCommand(Contract):
    """新建账号并在该作用域授予初始角色。"""

    kind: Literal["create_user"] = "create_user"
    user_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    tenant_id: BoundedId
    environment_id: BoundedId
    role: ProductRole


class SetUserStatusCommand(Contract):
    """启用或禁用一个账号。禁用是状态变更，不是删除行。"""

    kind: Literal["set_user_status"] = "set_user_status"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    status: UserStatus


class AssignRoleCommand(Contract):
    kind: Literal["assign_role"] = "assign_role"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    role: ProductRole


class RevokeRoleCommand(Contract):
    """撤销某作用域的角色。

    **没有 ``role`` 字段**：撤销的是"这个账号在这个作用域的角色"，不是"某个具体
    角色"。带上 role 就会出现"撤销 operator 但他其实是 admin"这种调用，而它要么
    静默无效、要么需要再加一条校验——后者又是"校验调用方有没有写错"。
    """

    kind: Literal["revoke_role"] = "revoke_role"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId


class BindExternalIdentityCommand(Contract):
    kind: Literal["bind_external_identity"] = "bind_external_identity"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    subject_ref: ControlledPiiField


class UnbindExternalIdentityCommand(Contract):
    kind: Literal["unbind_external_identity"] = "unbind_external_identity"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId


class BootstrapLocalAdminCommand(Contract):
    """首次把本地管理员凭据挂到一个目录账号上。

    它是 :data:`DirectoryCommand` 的成员而不是 ``LocalAdminStore`` 上的第二个写
    方法：``local_admins.user_id`` 是**授权事实**，授权事实只有一条写路径。
    """

    kind: Literal["bootstrap_local_admin"] = "bootstrap_local_admin"
    user_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    tenant_id: BoundedId
    environment_id: BoundedId
    password_hash: SecretHash = Field(exclude=True, repr=False)


class MigrateLegacyIdentitiesCommand(Contract):
    """整批旧身份迁移，**一个**命令一个事务。

    一条一条发就没有"整批原子"可言：中途失败会留下一半迁移过的目录，而那一半
    已经生效的授权没有任何地方记着它该被撤销。空批次是错误而不是 no-op——允许
    空批次就会有一条"迁移成功"的审计事件对应零次写入。
    """

    kind: Literal["migrate_legacy_identities"] = "migrate_legacy_identities"
    tenant_id: BoundedId
    environment_id: BoundedId
    entries: tuple[LegacyIdentityMigrationEntry, ...] = Field(
        min_length=1, max_length=_MAX_MIGRATION_ENTRIES
    )


DirectoryCommand: TypeAlias = Annotated[
    CreateUserCommand
    | SetUserStatusCommand
    | AssignRoleCommand
    | RevokeRoleCommand
    | BindExternalIdentityCommand
    | UnbindExternalIdentityCommand
    | BootstrapLocalAdminCommand
    | MigrateLegacyIdentitiesCommand,
    Field(discriminator="kind"),
]
"""八个写命令的判别联合。

它是 ``UserDirectoryStore.apply()`` 的**唯一**入参形状：本地管理员 bootstrap 与旧
身份迁移都必须是它的成员，不得另开写方法、另开事务或另写一份 SQL。
"""

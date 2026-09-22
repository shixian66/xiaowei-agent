"""身份目录的写边界，以及**审计事实的派生层**。

派生层是本模块存在的理由：``action``、``target_kind``、``target_ref_digest``、
``outcome`` 与 effect 全部由命令类型算出来，调用方连传都传不进来。能被调用方
指定，就能被调用方写错——一边撤销角色、一边记一条 ``user_created``，审计从此
只是一段与事实无关的文本，而且**不会有任何东西报错**。

``persistence`` 不能反向依赖 ``interfaces``（``tests/security/test_module_layering.py``），
因此这一层只认识契约里的十个命令，不认识飞书标签这类外部词汇。
"""

import hashlib
from collections.abc import Callable
from typing import Final, Protocol

from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditEffect,
    AdminAuditEvent,
    AdminOperationContext,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    ApproveActivationCommand,
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    DirectoryCommand,
    DirectoryPrincipalFacts,
    LegacyIdentityMigrationEntry,
    MigrateLegacyIdentitiesCommand,
    RejectActivationCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
)

BOOTSTRAP_ACTOR_USER_ID: Final[str] = "system-bootstrap"
"""本地管理员 bootstrap 的保留操作者。

它**不是**"这一次不写审计"的例外：bootstrap 发生时目录里还没有任何 Admin 账号，
操作者不可能是某个人。用例外会让"每一次授权改变都有审计"这句话需要附加条件，
而附加条件是审计体系瓦解的起点。
"""

BOOTSTRAP_OPERATION_ID: Final[str] = "bootstrap-local-admin"
"""bootstrap 的固定 operation id。

固定是安全的：写得出第二条 bootstrap 审计的前提是目录链接尚未建好，而链接一旦
建好，四态表第三行就让后续调用走 no-op、零审计。
"""

_SUBJECT_DIGEST_DOMAIN: Final[str] = "xiaowei.identity.external_subject.v1"
_ACTIVATION_USER_ID_DOMAIN: Final[str] = "xiaowei.identity.activation_user.v1"

ACTION_FOR_COMMAND: Final[dict[type, AdminAuditAction]] = {
    CreateUserCommand: AdminAuditAction.USER_CREATED,
    SetUserStatusCommand: AdminAuditAction.USER_STATUS_CHANGED,
    AssignRoleCommand: AdminAuditAction.ROLE_ASSIGNED,
    RevokeRoleCommand: AdminAuditAction.ROLE_REVOKED,
    BindExternalIdentityCommand: AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
    UnbindExternalIdentityCommand: AdminAuditAction.EXTERNAL_IDENTITY_UNBOUND,
    BootstrapLocalAdminCommand: AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
    MigrateLegacyIdentitiesCommand: AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
    ApproveActivationCommand: AdminAuditAction.ACTIVATION_APPROVED,
    RejectActivationCommand: AdminAuditAction.ACTIVATION_REJECTED,
}
"""命令类型 → 它必然记录的动作。绑死，不让调用方传。"""


def _no_effect(
    command: DirectoryCommand, entry: LegacyIdentityMigrationEntry | None
) -> AdminAuditEffect:
    """撤销与解绑之后，该作用域没有角色、账号状态也没变。

    记任何角色都是错的：读者会以为撤销之后还剩下点什么。
    """
    return AdminAuditEffect()


def _create_user_effect(
    command: CreateUserCommand, entry: LegacyIdentityMigrationEntry | None
) -> AdminAuditEffect:
    return AdminAuditEffect(role=command.role, status=UserStatus.ACTIVE)


def _status_effect(
    command: SetUserStatusCommand, entry: LegacyIdentityMigrationEntry | None
) -> AdminAuditEffect:
    return AdminAuditEffect(status=command.status)


def _assign_role_effect(
    command: AssignRoleCommand, entry: LegacyIdentityMigrationEntry | None
) -> AdminAuditEffect:
    return AdminAuditEffect(role=command.role)


def _bootstrap_effect(
    command: BootstrapLocalAdminCommand, entry: LegacyIdentityMigrationEntry | None
) -> AdminAuditEffect:
    """bootstrap 授予的角色是常量 ADMIN，不从命令里读——命令里根本没有这个字段。"""
    return AdminAuditEffect(role=ProductRole.ADMIN, status=UserStatus.ACTIVE)


def _migration_effect(
    command: MigrateLegacyIdentitiesCommand, entry: LegacyIdentityMigrationEntry | None
) -> AdminAuditEffect:
    """批量迁移的 effect 属于**条目**，不属于整批命令。

    整批只有一个角色的写法会让一次混合角色的迁移把所有人都记成同一个角色。
    """
    if entry is None:
        raise ValueError("a legacy migration effect needs the entry it migrated")
    return AdminAuditEffect(role=entry.role, status=UserStatus.ACTIVE)


def _approve_activation_effect(
    command: ApproveActivationCommand,
    entry: LegacyIdentityMigrationEntry | None,
) -> AdminAuditEffect:
    return AdminAuditEffect(role=command.approved_role, status=UserStatus.ACTIVE)


EFFECT_FOR_COMMAND: Final[dict[type, Callable[..., AdminAuditEffect]]] = {
    CreateUserCommand: _create_user_effect,
    SetUserStatusCommand: _status_effect,
    AssignRoleCommand: _assign_role_effect,
    RevokeRoleCommand: _no_effect,
    BindExternalIdentityCommand: _no_effect,
    UnbindExternalIdentityCommand: _no_effect,
    BootstrapLocalAdminCommand: _bootstrap_effect,
    MigrateLegacyIdentitiesCommand: _migration_effect,
    ApproveActivationCommand: _approve_activation_effect,
    RejectActivationCommand: _no_effect,
}
"""命令类型 → 算出成功时该记什么结果的纯函数。

闭集两个槽（角色、状态）与 :data:`~xiaowei_agent.contracts.admin_audit.ROLE_EFFECT_ACTIONS`
/ ``STATUS_EFFECT_ACTIONS`` 双向对齐，对不上时契约层直接拒绝构造。
"""


def external_subject_digest(
    *,
    provider: IdentitySource,
    tenant_id: str,
    environment_id: str,
    subject_ref: str,
) -> str:
    """外部主体引用的 domain-separated 摘要；数据库只见得到它。

    域与 ``admin_audit_target_digest`` **不同**。同域会让"某个 open_id 的绑定记录"
    与"针对该用户的审计事件"算出相等的摘要，于是拿到一张表就能反查另一张表的
    关联——两张表各自脱敏，合起来却不脱敏。

    作用域进摘要材料，所以同一个 open_id 在两个环境里是两个不同的摘要：一次
    环境级的泄漏不会顺带暴露另一个环境的绑定关系。
    """
    material = (
        f"{_SUBJECT_DIGEST_DOMAIN}\x1f{provider.value}\x1f{tenant_id}"
        f"\x1f{environment_id}\x1f{subject_ref}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def activation_user_id(*, subject_ref_digest: str) -> str:
    """从已作用域化的主体摘要派生固定长度账号 ID，不拼接外部明文。"""
    material = f"{_ACTIVATION_USER_ID_DOMAIN}\x1f{subject_ref_digest}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"feishu-{digest[:32]}"


def batch_operation_id(context: AdminOperationContext, index: int) -> str:
    """批量迁移里第 ``index`` 条的子 operation id（``index`` 从 1 开始）。

    根 id 的上限是 48、落库上限是 64，差出来的那 16 个字符正好装得下
    ``:10000``——这不是巧合，是 ``_MAX_MIGRATION_ENTRIES`` 与两个上限一起定出来的。
    """
    return f"{context.operation_id}:{index}"


def derive_audit(
    command: DirectoryCommand,
    context: AdminOperationContext,
    *,
    target_ref: str,
    operation_id: str | None = None,
    entry: LegacyIdentityMigrationEntry | None = None,
) -> AdminAuditCandidate:
    """从命令与上下文算出一条审计候选。**没有任何参数能指定结论。**

    ``operation_id`` 可覆盖，只为批量迁移的子 id；它仍然是从 ``context`` 派生的
    （见 :func:`batch_operation_id`），不是调用方另给的一个值。

    结果恒为 ``SUCCEEDED``：目录动作与它的审计在同一个事务里提交，失败的那一次
    整体回滚、一行不留，因此没有"失败的目录审计"这种事件。拒绝走
    ``AdminAuditStore.append_denied``，那是另一条路径。
    """
    command_type = type(command)
    if isinstance(command, (ApproveActivationCommand, RejectActivationCommand)):
        target_kind = AdminAuditTargetKind.ACTIVATION
        derived_target_ref = command.request_id
    else:
        target_kind = AdminAuditTargetKind.USER
        derived_target_ref = target_ref
    return AdminAuditCandidate(
        operation_id=context.operation_id if operation_id is None else operation_id,
        tenant_id=command.tenant_id,
        environment_id=command.environment_id,
        actor_user_id=context.actor_user_id,
        actor=context.actor,
        auth_source=context.auth_source,
        action=ACTION_FOR_COMMAND[command_type],
        target_kind=target_kind,
        target_ref_digest=admin_audit_target_digest(
            target_kind=target_kind, target_ref=derived_target_ref
        ),
        outcome=AdminAuditOutcome.SUCCEEDED,
        reason_code=None,
        effect=EFFECT_FOR_COMMAND[command_type](command, entry),
    )


class UserDirectoryError(RuntimeError):
    """身份目录的闭集错误基类；``RuntimeError`` 子类，能原样穿过两层收敛。"""


class UserDirectoryConflictError(UserDirectoryError):
    """目录事实冲突：actor 被占、主体已绑到别的账号、凭据链接破损。"""


class UserDirectoryNotFoundError(UserDirectoryError):
    """命令的目标账号或角色不存在。"""


class UserDirectoryDecisionDeniedError(UserDirectoryError):
    """激活决定被事务内权限/作用域校验拒绝。"""

    def __init__(self, reason_code: AdminAuditReasonCode) -> None:
        super().__init__("activation decision denied")
        self.reason_code = reason_code


class UserDirectorySubjectUnavailableError(UserDirectoryError):
    """主体已经绑定，但账号停用或当前作用域角色已撤销。"""


class AdminAuditUnwritableError(UserDirectoryError):
    """审计写不进去，因此这次**授权改变**失败。

    它属于目录错误族而不是审计错误族：失败的是 ``apply()`` 这个公开入口，而
    ``apply()`` 的失败只有一个意思——授权没有改成。
    """

    def __init__(self) -> None:
        super().__init__("the authorization change could not be audited")


class UserDirectoryStore(Protocol):
    """身份目录的读写边界；``apply`` 是四类授权事实的**唯一**写入口。

    唯一是结构上的：本地管理员 bootstrap 与旧身份迁移都是
    :data:`~xiaowei_agent.contracts.identity.DirectoryCommand` 的成员，不另开写
    方法、不另开事务、不另写一份 SQL。
    """

    async def load_account(
        self, *, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None:
        """读某个账号在某作用域的全部授权事实；缺账号或缺角色都返回 ``None``。"""

    async def resolve_by_subject(
        self,
        *,
        provider: IdentitySource,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
    ) -> DirectoryPrincipalFacts | None:
        """按**明文**主体引用解析。

        摘要化在 store 内部完成。让调用方自己算摘要，算错了就会永远判成"未绑定"
        ——而"未绑定"是一个不报错的结果。
        """

    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        """执行一次授权改变，并在**同一个事务**里写下派生出的审计。

        返回元组：批量迁移一次产生 N 条事件，单命令返回 1 元组，幂等 no-op 返回
        空元组。调用方用同一个形状处理全部情况——再开一个"批量专用"方法就等于
        再开一条写路径。
        """


__all__ = [
    "ACTION_FOR_COMMAND",
    "BOOTSTRAP_ACTOR_USER_ID",
    "BOOTSTRAP_OPERATION_ID",
    "EFFECT_FOR_COMMAND",
    "AdminAuditUnwritableError",
    "UserDirectoryConflictError",
    "UserDirectoryDecisionDeniedError",
    "UserDirectoryError",
    "UserDirectoryNotFoundError",
    "UserDirectoryStore",
    "UserDirectorySubjectUnavailableError",
    "activation_user_id",
    "batch_operation_id",
    "derive_audit",
    "external_subject_digest",
]

"""把旧静态身份文档一次性、原子地迁进数据库目录。

**这是 W1a 唯一对既有部署数据产生影响的一步。** 它只做三件事：按规格 §6.6 把原始
labels 映射成角色、把剩余条目装成**一个** :class:`MigrateLegacyIdentitiesCommand`、
调一次 :meth:`UserDirectoryStore.apply`。目录写入、审计派生与事务边界全部在切片 B
的 store 里，这里一行 SQL 都没有。

**整批原子由那一个事务给，不由这里的预检给。** 因此本模块不做任何"写之前先确认
写得进去"的检查——预检与写入之间有时间窗，而且预检通过也不等于写入成功。这里唯一
的读，是判断某个条目**是否已经完整迁移过**（那样的条目要从批次里剔除），而那个判
据必须自己 fail-closed：被剔除的条目不会进入批次，后面的唯一约束根本看不见它。

旧静态文件在迁移成功后**保留只读**一个发布周期：本模块不删除它，也不回头写它。
"""

import contextlib
import hashlib
from collections.abc import Iterator, Mapping
from typing import Final

from pydantic import StrictInt, StrictStr, ValidationError

from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.base import Contract
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole
from xiaowei_agent.contracts.identity import (
    LegacyIdentityMigrationEntry,
    MigrateLegacyIdentitiesCommand,
)
from xiaowei_agent.interfaces.feishu_identity import (
    LegacyIdentityEntry,
    read_legacy_identity_document,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryStore,
)

_USER_ID_DOMAIN: Final[str] = "xiaowei.identity.legacy_user.v1"
_OPERATION_ID_DOMAIN: Final[str] = "xiaowei.identity.legacy_migration.v1"
_USER_ID_PREFIX: Final[str] = "legacy-"
_OPERATION_ID_PREFIX: Final[str] = "legacy-migration-"
_USER_ID_DIGEST_CHARS: Final[int] = 32
_OPERATION_ID_DIGEST_CHARS: Final[int] = 16
_DISPLAY_NAME_MAX: Final[int] = 128

_ROLE_FOR_LABEL: Final[Mapping[str, ProductRole]] = {
    "admin": ProductRole.ADMIN,
    "operator": ProductRole.OPERATOR,
    "dba": ProductRole.OPERATOR,
    "oncall": ProductRole.OPERATOR,
    "viewer": ProductRole.USER,
    "approver": ProductRole.USER,
}
"""规格 §6.6 的映射表，逐行照抄。

``approver`` 映射到 ``USER`` 而**不是**某个审批角色：旧 approver 标签当前没有 ACL
真源，R1 交付结果 artifact 时才决定它的映射。现在给它任何高于 ``USER`` 的东西，都
是在凭空生效一份没有消费者的授权。它只进 :attr:`LegacyMigrationReport.deferred_labels`。
"""

_DEFERRED_LABELS: Final[frozenset[str]] = frozenset({"approver"})

_ROLE_RANK: Final[Mapping[ProductRole, int]] = {
    ProductRole.USER: 0,
    ProductRole.OPERATOR: 1,
    ProductRole.ADMIN: 2,
}
"""一条记录带多个标签时取**最高**角色。

排名写成显式表而不是靠枚举定义顺序：枚举顺序是给读者看的，改一下成员位置不会有
任何反馈，而这里改错会静默降权或静默提权。
"""


class LegacyMigrationConflictError(RuntimeError):
    """整批零写入。

    它是**唯一**会从本模块漏出去的失败形状：目录错误、审计不可写、以及组装 entry、
    命令与上下文时的契约拒绝全部收敛到它。理由是这几种情况对调用方是同一件事——
    "这批没迁成，库里一行都没变"。

    消息只取自闭集字面量，不拼接 actor、subject 或文件内容。
    """


@contextlib.contextmanager
def _contract_rejection_is_a_conflict() -> Iterator[None]:
    """契约拒绝的**唯一**收敛点。

    上一版只收敛了 entry 的组装，命令与上下文的组装没有，于是一个超出
    ``BoundedId`` 的 ``environment_id`` 会让原始的 pydantic ``ValidationError``
    直接漏给调用方：同一件事（这批装不进契约）却有两种错误分类，而调用方只
    ``except LegacyMigrationConflictError`` 时它会一路冒泡成未处理异常。

    收敛点做成一个，不是在两处各写一份 ``except``：各写一份就是两份判断，第三处
    组装出现时不会有任何东西提醒它也需要一份。
    """
    try:
        yield
    except ValidationError:
        # 不串接原因：分类错误的修复点是"它是不是一次整批失败"，而 pydantic 的
        # 内部错误结构不属于本模块的公开契约。
        raise LegacyMigrationConflictError(
            "a legacy entry does not fit the directory contract"
        ) from None


class LegacyMigrationReport(Contract):
    """一次迁移的结果。**不含任何主体引用**——明文 ``open_id`` 不进报告。"""

    created: StrictInt
    skipped: StrictInt
    deferred_labels: tuple[tuple[StrictStr, StrictStr], ...]
    audit_event_ids: tuple[StrictStr, ...]


def legacy_user_id(*, actor: str) -> str:
    """把旧 actor 算成一个有界的 ``user_id``。

    **不拼接 actor**：旧文档对 actor 没有任何长度上限，拼进去会让一个长 actor 在写
    库那一刻才炸，而且是在批量中途。摘要定长，因此这个函数的返回值不可能超限。
    """
    material = f"{_USER_ID_DOMAIN}\x1f{actor}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_USER_ID_PREFIX}{digest[:_USER_ID_DIGEST_CHARS]}"


def _operation_id(user_ids: tuple[str, ...]) -> str:
    """由**批次内容**派生的根 operation id。

    不用固定常量：固定常量会让第二次迁移（哪怕装的是全新条目）撞上同一个阶段键，
    于是"补迁一个新同事"这件合法的事被审计的 append-only 约束永久挡住。由内容派生
    则是：同一批重跑拿到同一个 id（它本来就该被拒），不同批拿到不同 id。
    """
    material = "\x1f".join((_OPERATION_ID_DOMAIN, *user_ids))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_OPERATION_ID_PREFIX}{digest[:_OPERATION_ID_DIGEST_CHARS]}"


def _role_for(entry: LegacyIdentityEntry) -> ProductRole:
    roles = [_ROLE_FOR_LABEL[label] for label in entry.labels]
    return max(roles, key=lambda role: _ROLE_RANK[role])


async def _is_already_migrated(
    *,
    directory: UserDirectoryStore,
    entry: LegacyIdentityEntry,
    user_id: str,
    role: ProductRole,
    tenant_id: str,
    environment_id: str,
) -> bool:
    """五项**全部精确匹配**才算迁过；任何一项不符都是冲突，不是 skip。

    这一步是最终判据，不是"报告用途"：判成 skip 的条目会从批次里剔除，后面那一个
    事务的唯一约束再也看不见它。凡是"先剔除、再让后面的约束兜底"的结构，剔除这一
    步就必须自己 fail-closed。
    """
    facts = await directory.load_account(
        user_id=user_id, tenant_id=tenant_id, environment_id=environment_id
    )
    if facts is None:
        # 三种情况落在这里：账号不在、账号在而该作用域没有角色、账号**被停用**。
        # `load_account` 对停用账号返回 `None`（"停用即刻生效"，由共享套件的
        # `test_disabled_account_stops_resolving_on_the_next_request` 在两个实现上
        # 各钉一遍），所以状态那一项不在下面**再判一次**——`facts` 非空时它必然是
        # ACTIVE，写在下面就是一条永远为假的分支。后两种情况会在批次里撞上账号
        # 唯一约束，由那一个事务给答案，仍然是整批零写入。
        return False
    if (
        facts.account.actor != entry.actor
        or facts.assignment.role is not role
    ):
        raise LegacyMigrationConflictError(
            "a legacy account already exists with different facts"
        )
    bound = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=tenant_id,
        environment_id=environment_id,
        subject_ref=entry.subject_ref,
    )
    if bound is None or bound.account.user_id != user_id:
        raise LegacyMigrationConflictError(
            "a legacy account exists without its matching feishu binding"
        )
    return True


async def migrate_static_identities(
    *,
    document_path: str,
    directory: UserDirectoryStore,
    tenant_id: str,
    environment_id: str,
    actor_user_id: str,
    actor: str,
) -> LegacyMigrationReport:
    """把旧文档里还没迁过的条目装成**一个**命令写进去。

    读文档用的是 :func:`~xiaowei_agent.interfaces.feishu_identity.read_legacy_identity_document`
    ——它保留原始 labels。走 ``load_feishu_identity_directory`` 拿不到分流所需的信息：
    labels 在那条路径上已经被压成权限位，``viewer`` 与 ``approver`` 压完完全一样。
    """
    entries = read_legacy_identity_document(
        path=document_path, tenant_id=tenant_id, environment_id=environment_id
    )

    pending: list[LegacyIdentityMigrationEntry] = []
    deferred: list[tuple[str, str]] = []
    skipped = 0
    for entry in entries:
        role = _role_for(entry)
        user_id = legacy_user_id(actor=entry.actor)
        deferred.extend(
            (entry.actor, label) for label in entry.labels if label in _DEFERRED_LABELS
        )
        if await _is_already_migrated(
            directory=directory,
            entry=entry,
            user_id=user_id,
            role=role,
            tenant_id=tenant_id,
            environment_id=environment_id,
        ):
            skipped += 1
            continue
        # 超出契约上限的取值在这里就被拒，发生在任何 apply() 之前，因此不存在
        # "写了一半"的状态。
        with _contract_rejection_is_a_conflict():
            pending.append(
                LegacyIdentityMigrationEntry(
                    user_id=user_id,
                    actor=entry.actor,
                    display_name=entry.actor[:_DISPLAY_NAME_MAX],
                    role=role,
                    subject_ref=entry.subject_ref,
                )
            )

    if not pending:
        return LegacyMigrationReport(
            created=0,
            skipped=skipped,
            deferred_labels=tuple(deferred),
            audit_event_ids=(),
        )

    # 作用域与操作者同样要过契约边界，走**同一个**收敛点：它们超限时也是一次
    # 整批零写入，不是另一类错误。
    with _contract_rejection_is_a_conflict():
        command = MigrateLegacyIdentitiesCommand(
            tenant_id=tenant_id,
            environment_id=environment_id,
            entries=tuple(pending),
        )
        context = AdminOperationContext(
            operation_id=_operation_id(tuple(item.user_id for item in pending)),
            actor_user_id=actor_user_id,
            actor=actor,
            auth_source=IdentitySource.LOCAL_ADMIN,
        )
    try:
        events = await directory.apply(command=command, context=context)
    except (UserDirectoryConflictError, AdminAuditUnwritableError) as error:
        raise LegacyMigrationConflictError(
            "the legacy migration batch was rejected by the directory"
        ) from error

    return LegacyMigrationReport(
        created=len(events),
        skipped=skipped,
        deferred_labels=tuple(deferred),
        audit_event_ids=tuple(event.event_id for event in events),
    )


__all__ = [
    "LegacyMigrationConflictError",
    "LegacyMigrationReport",
    "legacy_user_id",
    "migrate_static_identities",
]

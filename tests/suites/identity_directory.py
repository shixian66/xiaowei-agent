"""``UserDirectoryStore`` 与 ``AdminAuditStore`` 的跨实现行为用例。

内存与 PostgreSQL 两个绑定各跑一遍**同一份**用例。两个实现在矩阵一那张错误语义
表上必须逐格相同：内存没有事务，用快照恢复表达"回滚"那一列——它表达的是同一个
语义，不是另一个。

每个绑定必须提供六个 fixture：``directory``、``audit``、``admin_probe``、
``directory_probe``、``clock``，以及 ``broken_audit_derivation``（把该实现模块里的
``derive_audit`` 换成抛 ``RuntimeError`` 的桩，用来证明"与领域无关的异常同样不留痕"）。

``directory_probe`` 存在的理由是一次实测的验收假绿：回滚原本只通过
``load_account(...) is None`` 观察，而那是一次**组合读**——账号、角色两者缺一它就返回
``None``。于是逐个撤掉内存实现的六项快照恢复，契约用例仍然全绿，撤掉凭据恢复后全量
离线仍然全绿。承重的保护必须逐项可观测，否则"保护还在"和"保护没了"长得一模一样。
"""

from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest

from xiaowei_agent.contracts.activation import (
    ActivationLookup,
    ActivationSource,
    ActivationStatus,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditDenial,
    AdminAuditTerminal,
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
    LOCAL_ADMIN_ACTOR,
    LOCAL_ADMIN_DISPLAY_NAME,
    LOCAL_ADMIN_ENVIRONMENT_ID,
    LOCAL_ADMIN_TENANT_ID,
    LOCAL_ADMIN_USER_ID,
    ApproveActivationCommand,
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    ChangeManagedUserRoleCommand,
    CreateUserCommand,
    LegacyIdentityMigrationEntry,
    MigrateLegacyIdentitiesCommand,
    RejectActivationCommand,
    RevokeRoleCommand,
    SetManagedUserStatusCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
)
from xiaowei_agent.contracts.web_navigation import (
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.persistence.admin_audit import (
    AdminAuditConflictError,
    AdminAuditMissingStartError,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryDecisionDeniedError,
    UserDirectoryNotFoundError,
    UserDirectorySubjectUnavailableError,
    batch_operation_id,
    external_subject_digest,
)

TENANT = "tenant-a"
ENVIRONMENT = "env-a"
OTHER_ENVIRONMENT = "env-b"

_ADMIN_USER_ID = "operator-1"
_ADMIN_ACTOR = "carol@example.com"
_LEGACY_PASSWORD_HASH = "legacy" + "-argon2id-digest"
_FRESH_PASSWORD_HASH = "fresh" + "-argon2id-digest"


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    for case in cases:
        namespace[case.__name__] = case


def context(operation_id: str = "op-1") -> AdminOperationContext:
    return AdminOperationContext(
        operation_id=operation_id,
        actor_user_id=_ADMIN_USER_ID,
        actor=_ADMIN_ACTOR,
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


def admin_context(operation_id: str) -> AdminOperationContext:
    return AdminOperationContext(
        operation_id=operation_id,
        actor_user_id=LOCAL_ADMIN_USER_ID,
        actor=LOCAL_ADMIN_ACTOR,
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


def activation_command(
    subject_ref: str = "ou_pending", *, environment_id: str = ENVIRONMENT
) -> CreateActivationCommand:
    return CreateActivationCommand(
        tenant_id=TENANT,
        environment_id=environment_id,
        provider=IdentitySource.FEISHU,
        subject_ref=subject_ref,
        source=ActivationSource.WEB_LOGIN,
        return_intent=WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
    )


async def prepare_activation_admin(directory: Any) -> None:
    await directory.apply(command=bootstrap(), context=context("op-bootstrap"))
    await directory.apply(
        command=AssignRoleCommand(
            user_id=LOCAL_ADMIN_USER_ID,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            role=ProductRole.ADMIN,
        ),
        context=context("op-admin-scope"),
    )


def create_user(
    user_id: str = "alice",
    *,
    actor: str = "alice@example.com",
    role: ProductRole = ProductRole.USER,
    environment_id: str = ENVIRONMENT,
) -> CreateUserCommand:
    return CreateUserCommand(
        user_id=user_id,
        actor=actor,
        display_name="Alice",
        tenant_id=TENANT,
        environment_id=environment_id,
        role=role,
    )


def bootstrap(password_hash: str = _FRESH_PASSWORD_HASH) -> BootstrapLocalAdminCommand:
    return BootstrapLocalAdminCommand(
        user_id=LOCAL_ADMIN_USER_ID,
        actor=LOCAL_ADMIN_ACTOR,
        display_name=LOCAL_ADMIN_DISPLAY_NAME,
        tenant_id=LOCAL_ADMIN_TENANT_ID,
        environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
        password_hash=password_hash,
    )


def denial(operation_id: str = "op-denied") -> AdminAuditDenial:
    return AdminAuditDenial(
        operation_id=operation_id,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        actor_user_id=_ADMIN_USER_ID,
        actor=_ADMIN_ACTOR,
        auth_source=IdentitySource.LOCAL_ADMIN,
        action=AdminAuditAction.ROLE_ASSIGNED,
        target_kind=AdminAuditTargetKind.USER,
        target_ref_digest=admin_audit_target_digest(
            target_kind=AdminAuditTargetKind.USER, target_ref="alice"
        ),
        reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
    )


async def test_creating_a_user_persists_the_account_and_a_derived_audit_event(
    directory: Any, clock: Any
) -> None:
    events = await directory.apply(command=create_user(), context=context())

    assert len(events) == 1
    event = events[0]
    assert event.action is AdminAuditAction.USER_CREATED
    assert event.outcome is AdminAuditOutcome.SUCCEEDED
    assert event.created_at == clock()
    assert event.actor_user_id == _ADMIN_USER_ID

    facts = await directory.load_account(
        user_id="alice", tenant_id=TENANT, environment_id=ENVIRONMENT
    )
    assert facts is not None
    assert facts.account.actor == "alice@example.com"
    assert facts.account.status is UserStatus.ACTIVE
    assert facts.assignment.role is ProductRole.USER
    assert facts.assignment.created_by == _ADMIN_USER_ID


async def test_the_audit_event_points_at_the_user_the_command_changed(
    directory: Any,
) -> None:
    """目标摘要必须指向命令改的那个人，而不是操作者，也不是别人。"""
    (event,) = await directory.apply(command=create_user(), context=context())

    assert event.target_kind is AdminAuditTargetKind.USER
    assert event.target_ref_digest == admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref="alice"
    )
    assert event.target_ref_digest != admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref=_ADMIN_USER_ID
    )


async def test_role_assignment_records_which_role_was_granted(directory: Any) -> None:
    await directory.apply(command=create_user(), context=context("op-1"))

    (event,) = await directory.apply(
        command=AssignRoleCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            role=ProductRole.OPERATOR,
        ),
        context=context("op-2"),
    )

    assert event.action is AdminAuditAction.ROLE_ASSIGNED
    assert event.effect.role is ProductRole.OPERATOR
    assert event.effect.status is None
    facts = await directory.load_account(
        user_id="alice", tenant_id=TENANT, environment_id=ENVIRONMENT
    )
    assert facts is not None
    assert facts.assignment.role is ProductRole.OPERATOR


async def test_status_change_records_the_new_status(directory: Any) -> None:
    await directory.apply(command=create_user(), context=context("op-1"))

    (event,) = await directory.apply(
        command=SetUserStatusCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            status=UserStatus.DISABLED,
        ),
        context=context("op-2"),
    )

    assert event.action is AdminAuditAction.USER_STATUS_CHANGED
    assert event.effect.status is UserStatus.DISABLED
    assert event.effect.role is None


async def test_local_admin_can_manage_a_non_admin_with_expected_value_cas(
    directory: Any, directory_probe: Any
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=create_user("managed"), context=admin_context("op-create-managed")
    )
    before = await directory_probe.facts()

    (role_event,) = await directory.apply(
        command=ChangeManagedUserRoleCommand(
            user_id="managed",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            expected_role=ProductRole.USER,
            role=ProductRole.OPERATOR,
        ),
        context=admin_context("op-managed-role"),
    )
    (status_event,) = await directory.apply(
        command=SetManagedUserStatusCommand(
            user_id="managed",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            expected_status=UserStatus.ACTIVE,
            expected_role=ProductRole.OPERATOR,
            status=UserStatus.DISABLED,
        ),
        context=admin_context("op-managed-status"),
    )

    assert role_event.action is AdminAuditAction.ROLE_ASSIGNED
    assert role_event.effect.role is ProductRole.OPERATOR
    assert status_event.action is AdminAuditAction.USER_STATUS_CHANGED
    assert status_event.effect.status is UserStatus.DISABLED
    after = await directory_probe.facts()
    assert len(after["audits"] - before["audits"]) == 2


async def test_feishu_admin_can_manage_only_while_its_binding_is_current(
    directory: Any, directory_probe: Any
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=create_user(
            "feishu-admin", actor="feishu-admin@example.com", role=ProductRole.ADMIN
        ),
        context=admin_context("op-create-feishu-admin"),
    )
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="feishu-admin",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_feishu_admin",
        ),
        context=admin_context("op-bind-feishu-admin"),
    )
    await directory.apply(
        command=create_user("managed"), context=admin_context("op-create-managed")
    )
    feishu = AdminOperationContext(
        operation_id="op-feishu-manage",
        actor_user_id="feishu-admin",
        actor="feishu-admin@example.com",
        auth_source=IdentitySource.FEISHU,
    )

    await directory.apply(
        command=ChangeManagedUserRoleCommand(
            user_id="managed",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            expected_role=ProductRole.USER,
            role=ProductRole.OPERATOR,
        ),
        context=feishu,
    )
    await directory.apply(
        command=UnbindExternalIdentityCommand(
            user_id="feishu-admin",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        ),
        context=admin_context("op-unbind-feishu-admin"),
    )
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryDecisionDeniedError):
        await directory.apply(
            command=ChangeManagedUserRoleCommand(
                user_id="managed",
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                expected_role=ProductRole.OPERATOR,
                role=ProductRole.USER,
            ),
            context=feishu.model_copy(update={"operation_id": "op-feishu-revoked"}),
        )
    assert await directory_probe.facts() == before


@pytest.mark.parametrize(
    "case",
    ["ordinary", "disabled", "actor-mismatch", "forged-local", "stale-role"],
)
async def test_managed_write_revalidates_actor_and_expected_values(
    directory: Any, directory_probe: Any, case: str
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=create_user("managed"), context=admin_context("op-create-managed")
    )
    actor = admin_context(f"op-denied-{case}")
    if case == "ordinary":
        actor = context(f"op-denied-{case}")
    elif case == "disabled":
        await directory.apply(
            command=SetUserStatusCommand(
                user_id=LOCAL_ADMIN_USER_ID,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                status=UserStatus.DISABLED,
            ),
            context=admin_context("op-disable-admin"),
        )
    elif case == "actor-mismatch":
        actor = actor.model_copy(update={"actor": "forged@example.test"})
    elif case == "forged-local":
        await directory.apply(
            command=create_user(
                "forged-admin",
                actor="forged-admin@example.com",
                role=ProductRole.ADMIN,
            ),
            context=admin_context("op-create-forged-admin"),
        )
        actor = AdminOperationContext(
            operation_id=f"op-denied-{case}",
            actor_user_id="forged-admin",
            actor="forged-admin@example.com",
            auth_source=IdentitySource.LOCAL_ADMIN,
        )
    command = ChangeManagedUserRoleCommand(
        user_id="managed",
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        expected_role=(
            ProductRole.OPERATOR if case == "stale-role" else ProductRole.USER
        ),
        role=(ProductRole.USER if case == "stale-role" else ProductRole.OPERATOR),
    )
    before = await directory_probe.facts()

    expected_error = (
        UserDirectoryConflictError
        if case == "stale-role"
        else UserDirectoryDecisionDeniedError
    )
    with pytest.raises(expected_error):
        await directory.apply(command=command, context=actor)
    assert await directory_probe.facts() == before


@pytest.mark.parametrize("case", ["target-admin", "other-scope", "inactive-role"])
async def test_managed_write_refuses_protected_or_out_of_scope_targets(
    directory: Any, directory_probe: Any, case: str
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=create_user("managed", role=ProductRole.USER),
        context=admin_context("op-create-managed"),
    )
    if case in {"target-admin", "other-scope"}:
        await directory.apply(
            command=AssignRoleCommand(
                user_id="managed",
                tenant_id=TENANT,
                environment_id=OTHER_ENVIRONMENT,
                role=(
                    ProductRole.ADMIN
                    if case == "target-admin"
                    else ProductRole.USER
                ),
            ),
            context=admin_context("op-add-other-scope"),
        )
    if case == "inactive-role":
        await directory.apply(
            command=SetUserStatusCommand(
                user_id="managed",
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                status=UserStatus.DISABLED,
            ),
            context=admin_context("op-disable-managed"),
        )
    before = await directory_probe.facts()
    command: SetManagedUserStatusCommand | ChangeManagedUserRoleCommand
    if case in {"target-admin", "inactive-role"}:
        command = ChangeManagedUserRoleCommand(
            user_id="managed",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            expected_role=ProductRole.USER,
            role=ProductRole.OPERATOR,
        )
    else:
        command = SetManagedUserStatusCommand(
            user_id="managed",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            expected_status=UserStatus.ACTIVE,
            expected_role=ProductRole.USER,
            status=UserStatus.DISABLED,
        )

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(command=command, context=admin_context(f"op-{case}"))
    assert await directory_probe.facts() == before


async def test_revoking_a_role_claims_no_effect(directory: Any) -> None:
    """撤销之后该作用域没有角色，记任何角色都是错的。"""
    await directory.apply(command=create_user(), context=context("op-1"))

    (event,) = await directory.apply(
        command=RevokeRoleCommand(
            user_id="alice", tenant_id=TENANT, environment_id=ENVIRONMENT
        ),
        context=context("op-2"),
    )

    assert event.action is AdminAuditAction.ROLE_REVOKED
    assert event.effect.is_empty
    assert (
        await directory.load_account(
            user_id="alice", tenant_id=TENANT, environment_id=ENVIRONMENT
        )
        is None
    )


async def test_audit_write_failure_rolls_back_the_authorization_change(
    directory: Any, directory_probe: Any
) -> None:
    """**承重**：审计写不进去，授权改动一点都不留。

    复用同一个 ``operation_id`` 制造阶段冲突——这是真实调用方最容易撞上的那种
    重放，不是人为注入的异常。
    """
    await directory.apply(command=create_user("alice"), context=context("op-dup"))
    before = await directory_probe.facts()

    with pytest.raises(AdminAuditUnwritableError):
        await directory.apply(
            command=create_user("bob", actor="bob@example.com"),
            context=context("op-dup"),
        )

    after = await directory_probe.facts()
    for kind in ("accounts", "roles", "bindings", "audits"):
        assert after[kind] == before[kind], kind


async def test_any_failure_after_a_directory_write_leaves_nothing_behind(
    directory: Any, directory_probe: Any, broken_audit_derivation: Any
) -> None:
    """**承重**：与领域无关的异常同样不留半状态。

    只在两三种异常上恢复，审计派生、契约校验或 helper 抛出的别的异常就会留下
    "授权已改、审计没写"的半状态——而那一格恰好是本阶段最核心的不变量。

    **逐类事实分别断言**，不走 ``load_account`` 那一次组合读：组合读要账号与角色
    同时在才返回非空，因此"账号留下了、角色没留下"这种半状态它一个字都不会说。
    """
    before = await directory_probe.facts()
    broken_audit_derivation()

    with pytest.raises(RuntimeError):
        await directory.apply(command=create_user("carol"), context=context("op-boom"))

    after = await directory_probe.facts()
    for kind in ("accounts", "roles", "bindings", "audits"):
        assert after[kind] == before[kind], kind


async def test_two_accounts_cannot_claim_the_same_actor(directory: Any) -> None:
    await directory.apply(command=create_user("alice"), context=context("op-1"))

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=create_user("alice-2", actor="alice@example.com"),
            context=context("op-2"),
        )


@pytest.mark.parametrize("missing", ["assign", "status", "revoke", "bind", "unbind"])
async def test_commands_against_a_missing_account_are_not_found(
    directory: Any, missing: str
) -> None:
    commands = {
        "assign": AssignRoleCommand(
            user_id="ghost",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            role=ProductRole.USER,
        ),
        "status": SetUserStatusCommand(
            user_id="ghost",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            status=UserStatus.DISABLED,
        ),
        "revoke": RevokeRoleCommand(
            user_id="ghost", tenant_id=TENANT, environment_id=ENVIRONMENT
        ),
        "bind": BindExternalIdentityCommand(
            user_id="ghost",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_ghost",
        ),
        "unbind": UnbindExternalIdentityCommand(
            user_id="ghost", tenant_id=TENANT, environment_id=ENVIRONMENT
        ),
    }
    with pytest.raises(UserDirectoryNotFoundError):
        await directory.apply(command=commands[missing], context=context())


async def test_one_subject_ref_cannot_bind_two_accounts(directory: Any) -> None:
    await directory.apply(command=create_user("alice"), context=context("op-1"))
    await directory.apply(
        command=create_user("bob", actor="bob@example.com"), context=context("op-2")
    )
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_shared",
        ),
        context=context("op-3"),
    )

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=BindExternalIdentityCommand(
                user_id="bob",
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                subject_ref="ou_shared",
            ),
            context=context("op-4"),
        )


async def test_disabled_bound_account_is_distinct_from_an_unknown_subject(
    directory: Any,
) -> None:
    """停用即刻生效：解析不再返回任何授权事实。"""
    await directory.apply(command=create_user("alice"), context=context("op-1"))
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_alice",
        ),
        context=context("op-2"),
    )
    assert (
        await directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_alice",
        )
        is not None
    )

    await directory.apply(
        command=SetUserStatusCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            status=UserStatus.DISABLED,
        ),
        context=context("op-3"),
    )

    with pytest.raises(UserDirectorySubjectUnavailableError):
        await directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_alice",
        )


async def test_bound_account_without_a_current_scope_role_is_unavailable(
    directory: Any,
) -> None:
    await directory.apply(command=create_user("alice"), context=context("op-1"))
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_alice",
        ),
        context=context("op-2"),
    )
    await directory.apply(
        command=RevokeRoleCommand(
            user_id="alice", tenant_id=TENANT, environment_id=ENVIRONMENT
        ),
        context=context("op-3"),
    )

    with pytest.raises(UserDirectorySubjectUnavailableError):
        await directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_alice",
        )


async def test_a_binding_in_one_scope_does_not_resolve_in_another(
    directory: Any,
) -> None:
    await directory.apply(command=create_user("alice"), context=context("op-1"))
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_alice",
        ),
        context=context("op-2"),
    )

    assert (
        await directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=TENANT,
            environment_id=OTHER_ENVIRONMENT,
            subject_ref="ou_alice",
        )
        is None
    )


async def test_unknown_subject_resolves_to_nothing(directory: Any) -> None:
    """未知主体不报错、不返回——报错会把"这个 open_id 在不在库里"变成可探测的。"""
    assert (
        await directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_nobody",
        )
        is None
    )


async def test_bootstrap_creates_account_role_and_credential_together(
    directory: Any, admin_probe: Any
) -> None:
    (event,) = await directory.apply(command=bootstrap(), context=context("op-boot"))

    assert event.action is AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED
    assert event.effect.role is ProductRole.ADMIN
    assert event.effect.status is UserStatus.ACTIVE
    facts = await directory.load_account(
        user_id=LOCAL_ADMIN_USER_ID,
        tenant_id=LOCAL_ADMIN_TENANT_ID,
        environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
    )
    assert facts is not None
    assert facts.assignment.role is ProductRole.ADMIN
    assert await admin_probe.linked_user_id() == LOCAL_ADMIN_USER_ID
    assert await admin_probe.stored_password_hash() == _FRESH_PASSWORD_HASH


async def test_bootstrap_is_idempotent_and_writes_no_audit_when_nothing_changed(
    directory: Any,
) -> None:
    """链接完整时是 no-op：返回空元组，**且不写审计**。

    写一条"什么都没改"的成功审计，会让审计表里的 bootstrap 条数与真实 bootstrap
    次数脱钩，而那正是事后要数"这套部署被重新初始化过几次"时唯一的依据。
    """
    await directory.apply(command=bootstrap(), context=context("op-boot"))

    assert await directory.apply(command=bootstrap(), context=context("op-boot-2")) == ()


async def test_bootstrap_backfills_a_credential_that_has_no_directory_account(
    directory: Any, admin_probe: Any
) -> None:
    """四态表第二行：跑过 RI5 的库早就有这一行，而它的 ``user_id`` 是 NULL。

    同时钉住**不覆盖原口令**：管理员可能早就改过密码，backfill 拿传进来的初始
    口令覆盖回去，等于一次静默的凭据回滚。
    """
    await admin_probe.seed_legacy_credential(password_hash=_LEGACY_PASSWORD_HASH)

    (event,) = await directory.apply(command=bootstrap(), context=context("op-boot"))

    assert event.action is AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED
    assert await admin_probe.linked_user_id() == LOCAL_ADMIN_USER_ID
    assert await admin_probe.stored_password_hash() == _LEGACY_PASSWORD_HASH
    facts = await directory.load_account(
        user_id=LOCAL_ADMIN_USER_ID,
        tenant_id=LOCAL_ADMIN_TENANT_ID,
        environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
    )
    assert facts is not None
    assert facts.assignment.role is ProductRole.ADMIN


async def test_bootstrap_backfill_is_itself_idempotent(
    directory: Any, admin_probe: Any
) -> None:
    await admin_probe.seed_legacy_credential(password_hash=_LEGACY_PASSWORD_HASH)
    await directory.apply(command=bootstrap(), context=context("op-boot"))

    assert await directory.apply(command=bootstrap(), context=context("op-boot-2")) == ()
    assert await admin_probe.stored_password_hash() == _LEGACY_PASSWORD_HASH


async def test_bootstrap_fails_closed_when_the_link_points_at_nothing(
    directory: Any, admin_probe: Any
) -> None:
    """四态表第四行之一：链接指向一个不存在的账号。

    这时**不能**当作"还没升级"去补写：补写会让一行悬空的凭据重新拿到 ADMIN，
    而它原本指向的那个账号是谁、为什么不见了，没有任何地方记着。

    两个实现在这一格上的**强度不同**，因此断言写在不变量上而不是写在机制上：
    PostgreSQL 的 ``fk_local_admins_user_id`` 是 ``RESTRICT``，悬空链接那一行
    根本插不进去；内存没有外键，摆得出来，于是必须由 store 自己 fail-closed。
    探针如实返回它有没有造出这个状态，两条分支各自钉住"悬空链接绝不会通过
    一次 bootstrap"。
    """
    forged = await admin_probe.seed_legacy_credential(
        password_hash=_LEGACY_PASSWORD_HASH, user_id="vanished"
    )

    if forged:
        with pytest.raises(UserDirectoryConflictError):
            await directory.apply(command=bootstrap(), context=context("op-boot"))
        assert await admin_probe.linked_user_id() == "vanished"
        return
    # 数据库自己就不接受这一行——比 store 的 fail-closed 更强一档。探针已经证明
    # 插入被拒，这里确认它确实什么都没留下。
    assert await admin_probe.linked_user_id() is None


async def test_a_failed_bootstrap_leaves_the_credential_row_untouched(
    directory: Any, admin_probe: Any, directory_probe: Any, broken_audit_derivation: Any
) -> None:
    """**承重**：快照必须覆盖 ``state.local_admin``，不只是四张目录表。

    bootstrap 在同一次调用里既写凭据又写目录。审计失败时只回滚目录、不回滚凭据，
    会留下一行 ``user_id`` 已被写上的凭据——而那一行正好落进四态表第三行（幂等
    no-op）而不是第二行（backfill），于是**下一次装配什么都不会做**，目录里永远没有
    这个管理员账号。这条裂缝不会让任何一张表看起来异常。

    四张目录表另有别的用例把守；这一条专钉凭据行那一项。
    """
    await admin_probe.seed_legacy_credential(password_hash=_LEGACY_PASSWORD_HASH)
    before = await directory_probe.facts()
    broken_audit_derivation()

    with pytest.raises(RuntimeError):
        await directory.apply(command=bootstrap(), context=context("op-boom"))

    assert await admin_probe.linked_user_id() is None
    assert await admin_probe.stored_password_hash() == _LEGACY_PASSWORD_HASH
    after = await directory_probe.facts()
    for kind in ("accounts", "roles", "bindings", "audits"):
        assert after[kind] == before[kind], kind


async def test_bootstrap_fails_closed_when_the_linked_account_lost_admin(
    directory: Any, admin_probe: Any
) -> None:
    """四态表第四行之二：账号在，但它在该作用域已经没有 ADMIN 角色了。"""
    await directory.apply(command=bootstrap(), context=context("op-boot"))
    await directory.apply(
        command=AssignRoleCommand(
            user_id=LOCAL_ADMIN_USER_ID,
            tenant_id=LOCAL_ADMIN_TENANT_ID,
            environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
            role=ProductRole.OPERATOR,
        ),
        context=context("op-demote"),
    )

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(command=bootstrap(), context=context("op-boot-2"))


def _entry(user_id: str, *, role: ProductRole = ProductRole.USER) -> LegacyIdentityMigrationEntry:
    return LegacyIdentityMigrationEntry(
        user_id=user_id,
        actor=f"{user_id}@example.com",
        display_name=user_id.title(),
        role=role,
        subject_ref=f"ou_{user_id}",
    )


async def test_legacy_migration_writes_the_whole_batch_or_nothing(
    directory: Any, directory_probe: Any
) -> None:
    """整批原子：第二条撞上已被占用的 actor，第一条也不能留下。

    这条是**唯一**一条会让第一项写入真正落进四类事实再被撤回的用例：第一条目的
    账号、角色、绑定、审计事件与阶段键全部写过一遍。因此四类事实逐个比对，外加一条
    "同一个根 operation id 可以重新跑一遍"——阶段键如果没还回去，重放会撞上
    ``AdminAuditUnwritableError``，而那时四张表看起来完全干净，谁也不会怀疑到它。
    """
    await directory.apply(
        command=create_user("squatter", actor="dave@example.com"),
        context=context("op-1"),
    )
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=MigrateLegacyIdentitiesCommand(
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                entries=(_entry("erin"), _entry("dave")),
            ),
            context=context("op-migrate"),
        )

    after = await directory_probe.facts()
    for kind in ("accounts", "roles", "bindings", "audits"):
        assert after[kind] == before[kind], kind

    replayed = await directory.apply(
        command=MigrateLegacyIdentitiesCommand(
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            entries=(_entry("erin"), _entry("frank")),
        ),
        context=context("op-migrate"),
    )
    assert len(replayed) == 2


async def test_legacy_migration_emits_one_audit_event_per_entry(
    directory: Any, audit: Any
) -> None:
    """每条一个审计，子 operation id 由根 id 派生且有界。"""
    root = context("op-migrate")
    events = await directory.apply(
        command=MigrateLegacyIdentitiesCommand(
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            entries=(_entry("erin"), _entry("frank", role=ProductRole.OPERATOR)),
        ),
        context=root,
    )

    assert len(events) == 2
    assert [event.operation_id for event in events] == [
        batch_operation_id(root, 1),
        batch_operation_id(root, 2),
    ]
    assert [event.effect.role for event in events] == [
        ProductRole.USER,
        ProductRole.OPERATOR,
    ]
    assert all(len(event.operation_id) <= 64 for event in events)
    for event in events:
        assert await audit.load(event_id=event.event_id) == event

    resolved = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        subject_ref="ou_erin",
    )
    assert resolved is not None
    assert resolved.account.user_id == "erin"
    assert external_subject_digest(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        subject_ref="ou_erin",
    ) != admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref="ou_erin"
    )


async def test_a_second_event_at_the_same_stage_is_rejected(audit: Any) -> None:
    """直接调 ``AdminAuditStore`` 时，同一个事实是 ``AdminAuditConflictError``。

    用两次 ``append_denied`` 而不是两次 ``append_started``：W1a 的八个动作全是目录
    动作，``AdminAuditStart`` 在契约层就拒绝它们，因此本阶段根本写不出 STARTED。
    """
    await audit.append_denied(denial=denial("op-denied"))

    with pytest.raises(AdminAuditConflictError):
        await audit.append_denied(denial=denial("op-denied"))


async def test_a_denied_event_records_the_refusal_without_claiming_an_effect(
    audit: Any, clock: Any
) -> None:
    event = await audit.append_denied(denial=denial())

    assert event.outcome is AdminAuditOutcome.DENIED
    assert event.reason_code is AdminAuditReasonCode.ACTOR_NOT_ADMIN
    assert event.effect.is_empty
    assert event.created_at == clock()
    assert await audit.load(event_id=event.event_id) == event


async def test_a_terminal_event_without_a_started_event_is_refused(audit: Any) -> None:
    with pytest.raises(AdminAuditMissingStartError):
        await audit.append_terminal(
            terminal=AdminAuditTerminal(
                operation_id="op-never-started",
                outcome=AdminAuditOutcome.FAILED,
                reason_code=AdminAuditReasonCode.CONFLICT,
            )
        )


async def test_activation_approval_is_atomic_and_derives_the_audit_matrix(
    directory: Any, activation_store: Any
) -> None:
    await prepare_activation_admin(directory)
    pending = await activation_store.create_or_reuse(command=activation_command())

    (event,) = await directory.apply(
        command=ApproveActivationCommand(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            actor="new@example.com",
            display_name="New User",
            approved_role=ProductRole.OPERATOR,
        ),
        context=admin_context("op-approve"),
    )

    decided = await activation_store.load(
        query=ActivationLookup(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        )
    )
    assert decided is not None
    assert decided.status is ActivationStatus.APPROVED
    assert decided.approved_role is ProductRole.OPERATOR
    resolved = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        subject_ref="ou_pending",
    )
    assert resolved is not None
    assert resolved.account.actor == "new@example.com"
    assert resolved.assignment.role is ProductRole.OPERATOR
    assert event.action is AdminAuditAction.ACTIVATION_APPROVED
    assert event.target_kind is AdminAuditTargetKind.ACTIVATION
    assert event.effect.role is ProductRole.OPERATOR
    assert event.effect.status is UserStatus.ACTIVE


async def test_activation_approval_reuses_an_active_account_with_the_same_actor(
    directory: Any, activation_store: Any, directory_probe: Any
) -> None:
    """既有账号是 actor 的全局真源；批准不能为同一 actor 再造一个摘要 ID。"""
    await prepare_activation_admin(directory)
    await directory.apply(
        command=CreateUserCommand(
            user_id="legacy-alice",
            actor="alice@example.com",
            display_name="Alice",
            tenant_id=TENANT,
            environment_id="other-env",
            role=ProductRole.USER,
        ),
        context=admin_context("op-create-existing-actor"),
    )
    pending = await activation_store.create_or_reuse(command=activation_command())

    await directory.apply(
        command=ApproveActivationCommand(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            actor="alice@example.com",
            display_name="Alice",
        ),
        context=admin_context("op-approve-existing-actor"),
    )

    resolved = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        subject_ref="ou_pending",
    )
    assert resolved is not None
    assert resolved.account.user_id == "legacy-alice"
    assert resolved.assignment.role is ProductRole.USER
    facts = await directory_probe.facts()
    assert "legacy-alice" in facts["accounts"]
    assert len(facts["accounts"]) == 2  # local admin + reused account


async def test_activation_approval_keeps_an_existing_matching_role(
    directory: Any, activation_store: Any
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=CreateUserCommand(
            user_id="existing-user",
            actor="existing@example.com",
            display_name="Existing User",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            role=ProductRole.USER,
        ),
        context=admin_context("op-create-existing-role"),
    )
    pending = await activation_store.create_or_reuse(command=activation_command())

    await directory.apply(
        command=ApproveActivationCommand(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            actor="existing@example.com",
            display_name="Existing User",
            approved_role=ProductRole.USER,
        ),
        context=admin_context("op-approve-existing-role"),
    )

    resolved = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        subject_ref="ou_pending",
    )
    assert resolved is not None
    assert resolved.account.user_id == "existing-user"
    assert resolved.assignment.role is ProductRole.USER


@pytest.mark.parametrize(
    ("existing_display_name", "existing_status"),
    [
        ("Different Alice", UserStatus.ACTIVE),
        ("Alice", UserStatus.DISABLED),
    ],
)
async def test_activation_approval_refuses_an_incompatible_existing_actor(
    directory: Any,
    activation_store: Any,
    directory_probe: Any,
    existing_display_name: str,
    existing_status: UserStatus,
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=CreateUserCommand(
            user_id="legacy-alice",
            actor="alice@example.com",
            display_name=existing_display_name,
            tenant_id=TENANT,
            environment_id="other-env",
            role=ProductRole.USER,
        ),
        context=admin_context("op-create-incompatible-actor"),
    )
    if existing_status is UserStatus.DISABLED:
        await directory.apply(
            command=SetUserStatusCommand(
                user_id="legacy-alice",
                tenant_id=TENANT,
                environment_id="other-env",
                status=UserStatus.DISABLED,
            ),
            context=admin_context("op-disable-existing-actor"),
        )
    pending = await activation_store.create_or_reuse(command=activation_command())
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=ApproveActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                actor="alice@example.com",
                display_name="Alice",
            ),
            context=admin_context("op-approve-incompatible-actor"),
        )

    assert await directory_probe.facts() == before


@pytest.mark.parametrize("existing_role", [ProductRole.OPERATOR, ProductRole.ADMIN])
async def test_activation_approval_never_overwrites_an_existing_role(
    directory: Any,
    activation_store: Any,
    directory_probe: Any,
    existing_role: ProductRole,
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=CreateUserCommand(
            user_id="legacy-alice",
            actor="alice@example.com",
            display_name="Alice",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            role=existing_role,
        ),
        context=admin_context("op-create-role-conflict"),
    )
    pending = await activation_store.create_or_reuse(command=activation_command())
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=ApproveActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                actor="alice@example.com",
                display_name="Alice",
                approved_role=ProductRole.USER,
            ),
            context=admin_context("op-approve-role-conflict"),
        )

    assert await directory_probe.facts() == before


async def test_activation_approval_rolls_back_if_the_subject_became_bound(
    directory: Any, activation_store: Any, directory_probe: Any
) -> None:
    """申请后出现的身份绑定由事务内事实裁决，不能留下半个新账号。"""
    await prepare_activation_admin(directory)
    pending = await activation_store.create_or_reuse(command=activation_command())
    await directory.apply(
        command=CreateUserCommand(
            user_id="existing-owner",
            actor="owner@example.com",
            display_name="Existing Owner",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            role=ProductRole.USER,
        ),
        context=admin_context("op-create-existing-owner"),
    )
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="existing-owner",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_pending",
        ),
        context=admin_context("op-bind-existing-owner"),
    )
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=ApproveActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                actor="new@example.com",
                display_name="New User",
            ),
            context=admin_context("op-approve-bound-subject"),
        )

    assert await directory_probe.facts() == before


async def test_same_subject_can_be_approved_in_two_scopes_without_fact_leakage(
    directory: Any, activation_store: Any
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=AssignRoleCommand(
            user_id=LOCAL_ADMIN_USER_ID,
            tenant_id=TENANT,
            environment_id=OTHER_ENVIRONMENT,
            role=ProductRole.ADMIN,
        ),
        context=admin_context("op-admin-other-environment"),
    )
    first = await activation_store.create_or_reuse(command=activation_command())
    second = await activation_store.create_or_reuse(
        command=activation_command(environment_id=OTHER_ENVIRONMENT)
    )

    for request, environment, operation_id in (
        (first, ENVIRONMENT, "op-approve-first-scope"),
        (second, OTHER_ENVIRONMENT, "op-approve-second-scope"),
    ):
        await directory.apply(
            command=ApproveActivationCommand(
                request_id=request.request_id,
                tenant_id=TENANT,
                environment_id=environment,
                actor="same@example.com",
                display_name="Same User",
            ),
            context=admin_context(operation_id),
        )

    first_resolved = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
        subject_ref="ou_pending",
    )
    second_resolved = await directory.resolve_by_subject(
        provider=IdentitySource.FEISHU,
        tenant_id=TENANT,
        environment_id=OTHER_ENVIRONMENT,
        subject_ref="ou_pending",
    )
    assert first_resolved is not None
    assert second_resolved is not None
    assert first_resolved.account.user_id == second_resolved.account.user_id
    assert first_resolved.assignment.environment_id == ENVIRONMENT
    assert second_resolved.assignment.environment_id == OTHER_ENVIRONMENT


async def test_activation_rejection_writes_only_terminal_and_empty_effect(
    directory: Any, activation_store: Any
) -> None:
    await prepare_activation_admin(directory)
    pending = await activation_store.create_or_reuse(command=activation_command())

    (event,) = await directory.apply(
        command=RejectActivationCommand(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        ),
        context=admin_context("op-reject"),
    )

    decided = await activation_store.load(
        query=ActivationLookup(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        )
    )
    assert decided is not None
    assert decided.status is ActivationStatus.REJECTED
    assert event.action is AdminAuditAction.ACTIVATION_REJECTED
    assert event.target_kind is AdminAuditTargetKind.ACTIVATION
    assert event.effect.is_empty


async def test_activation_decision_revalidates_the_current_admin(
    directory: Any, activation_store: Any
) -> None:
    pending = await activation_store.create_or_reuse(command=activation_command())

    with pytest.raises(UserDirectoryDecisionDeniedError) as caught:
        await directory.apply(
            command=RejectActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
            ),
            context=context("op-not-admin"),
        )

    assert caught.value.reason_code is AdminAuditReasonCode.ACTOR_NOT_ADMIN
    still_pending = await activation_store.load(
        query=ActivationLookup(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        )
    )
    assert still_pending is not None
    assert still_pending.status is ActivationStatus.PENDING


async def test_activation_decision_hides_a_request_from_another_scope(
    directory: Any, activation_store: Any, directory_probe: Any
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=AssignRoleCommand(
            user_id=LOCAL_ADMIN_USER_ID,
            tenant_id=TENANT,
            environment_id="other-env",
            role=ProductRole.ADMIN,
        ),
        context=admin_context("op-admin-other-scope"),
    )
    pending = await activation_store.create_or_reuse(command=activation_command())
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryDecisionDeniedError) as caught:
        await directory.apply(
            command=RejectActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id="other-env",
            ),
            context=admin_context("op-wrong-scope"),
        )

    assert caught.value.reason_code is AdminAuditReasonCode.SCOPE_MISMATCH
    assert await directory_probe.facts() == before


@pytest.mark.parametrize(
    "case",
    ["ordinary-user", "disabled-admin", "actor-mismatch", "external-auth"],
)
async def test_activation_decision_rejects_every_non_current_admin_shape(
    directory: Any,
    activation_store: Any,
    directory_probe: Any,
    case: str,
) -> None:
    await prepare_activation_admin(directory)
    pending = await activation_store.create_or_reuse(command=activation_command())
    decision_context = admin_context(f"op-denied-{case}")
    expected = AdminAuditReasonCode.ACTOR_NOT_ADMIN

    if case == "ordinary-user":
        await directory.apply(
            command=CreateUserCommand(
                user_id="ordinary",
                actor="ordinary@example.com",
                display_name="Ordinary",
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                role=ProductRole.USER,
            ),
            context=admin_context("op-create-ordinary"),
        )
        decision_context = AdminOperationContext(
            operation_id="op-denied-ordinary",
            actor_user_id="ordinary",
            actor="ordinary@example.com",
            auth_source=IdentitySource.LOCAL_ADMIN,
        )
    elif case == "disabled-admin":
        await directory.apply(
            command=SetUserStatusCommand(
                user_id=LOCAL_ADMIN_USER_ID,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                status=UserStatus.DISABLED,
            ),
            context=admin_context("op-disable-admin"),
        )
    elif case == "actor-mismatch":
        decision_context = AdminOperationContext(
            operation_id="op-denied-actor",
            actor_user_id=LOCAL_ADMIN_USER_ID,
            actor="not-the-admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
        )
    else:
        decision_context = AdminOperationContext(
            operation_id="op-denied-source",
            actor_user_id=LOCAL_ADMIN_USER_ID,
            actor=LOCAL_ADMIN_ACTOR,
            auth_source=IdentitySource.FEISHU,
        )
        expected = AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED

    before = await directory_probe.facts()
    with pytest.raises(UserDirectoryDecisionDeniedError) as caught:
        await directory.apply(
            command=RejectActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
            ),
            context=decision_context,
        )

    assert caught.value.reason_code is expected
    assert await directory_probe.facts() == before


async def test_terminal_activation_replay_is_a_conflict_without_side_effects(
    directory: Any, activation_store: Any, directory_probe: Any
) -> None:
    await prepare_activation_admin(directory)
    pending = await activation_store.create_or_reuse(command=activation_command())
    command = RejectActivationCommand(
        request_id=pending.request_id,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
    )
    await directory.apply(command=command, context=admin_context("op-reject"))
    before = await directory_probe.facts()

    with pytest.raises(UserDirectoryConflictError):
        await directory.apply(
            command=command, context=admin_context("op-reject-replay")
        )
    assert await directory_probe.facts() == before


async def test_activation_approval_rolls_back_the_request_with_the_directory(
    directory: Any,
    activation_store: Any,
    directory_probe: Any,
    broken_audit_derivation: Any,
) -> None:
    await prepare_activation_admin(directory)
    pending = await activation_store.create_or_reuse(command=activation_command())
    before = await directory_probe.facts()
    broken_audit_derivation()

    with pytest.raises(RuntimeError):
        await directory.apply(
            command=ApproveActivationCommand(
                request_id=pending.request_id,
                tenant_id=TENANT,
                environment_id=ENVIRONMENT,
                actor="new@example.com",
                display_name="New User",
                approved_role=ProductRole.USER,
            ),
            context=admin_context("op-approve"),
        )

    assert await directory_probe.facts() == before
    still_pending = await activation_store.load(
        query=ActivationLookup(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        )
    )
    assert still_pending is not None
    assert still_pending.status is ActivationStatus.PENDING


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_feishu_admin_can_make_both_activation_decisions(
    directory: Any, activation_store: Any, decision: str
) -> None:
    await prepare_activation_admin(directory)
    await directory.apply(
        command=create_user(
            "feishu-admin", actor="feishu-admin@example.com", role=ProductRole.ADMIN
        ),
        context=admin_context("op-create-feishu-admin"),
    )
    await directory.apply(
        command=BindExternalIdentityCommand(
            user_id="feishu-admin",
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            subject_ref="ou_feishu_admin",
        ),
        context=admin_context("op-bind-feishu-admin"),
    )
    pending = await activation_store.create_or_reuse(
        command=activation_command(f"ou_{decision}")
    )
    actor = AdminOperationContext(
        operation_id=f"op-feishu-{decision}",
        actor_user_id="feishu-admin",
        actor="feishu-admin@example.com",
        auth_source=IdentitySource.FEISHU,
    )
    command = (
        ApproveActivationCommand(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
            actor="approved@example.com",
            display_name="Approved User",
        )
        if decision == "approve"
        else RejectActivationCommand(
            request_id=pending.request_id,
            tenant_id=TENANT,
            environment_id=ENVIRONMENT,
        )
    )

    (event,) = await directory.apply(command=command, context=actor)

    assert event.auth_source is IdentitySource.FEISHU
    assert event.action is (
        AdminAuditAction.ACTIVATION_APPROVED
        if decision == "approve"
        else AdminAuditAction.ACTIVATION_REJECTED
    )


IDENTITY_DIRECTORY_CASES = (
    test_creating_a_user_persists_the_account_and_a_derived_audit_event,
    test_the_audit_event_points_at_the_user_the_command_changed,
    test_role_assignment_records_which_role_was_granted,
    test_status_change_records_the_new_status,
    test_local_admin_can_manage_a_non_admin_with_expected_value_cas,
    test_feishu_admin_can_manage_only_while_its_binding_is_current,
    test_managed_write_revalidates_actor_and_expected_values,
    test_managed_write_refuses_protected_or_out_of_scope_targets,
    test_revoking_a_role_claims_no_effect,
    test_audit_write_failure_rolls_back_the_authorization_change,
    test_any_failure_after_a_directory_write_leaves_nothing_behind,
    test_two_accounts_cannot_claim_the_same_actor,
    test_commands_against_a_missing_account_are_not_found,
    test_one_subject_ref_cannot_bind_two_accounts,
    test_disabled_bound_account_is_distinct_from_an_unknown_subject,
    test_bound_account_without_a_current_scope_role_is_unavailable,
    test_a_binding_in_one_scope_does_not_resolve_in_another,
    test_unknown_subject_resolves_to_nothing,
    test_bootstrap_creates_account_role_and_credential_together,
    test_bootstrap_is_idempotent_and_writes_no_audit_when_nothing_changed,
    test_bootstrap_backfills_a_credential_that_has_no_directory_account,
    test_bootstrap_backfill_is_itself_idempotent,
    test_bootstrap_fails_closed_when_the_link_points_at_nothing,
    test_a_failed_bootstrap_leaves_the_credential_row_untouched,
    test_bootstrap_fails_closed_when_the_linked_account_lost_admin,
    test_legacy_migration_writes_the_whole_batch_or_nothing,
    test_legacy_migration_emits_one_audit_event_per_entry,
    test_a_second_event_at_the_same_stage_is_rejected,
    test_a_denied_event_records_the_refusal_without_claiming_an_effect,
    test_a_terminal_event_without_a_started_event_is_refused,
    test_activation_approval_is_atomic_and_derives_the_audit_matrix,
    test_activation_approval_reuses_an_active_account_with_the_same_actor,
    test_activation_approval_keeps_an_existing_matching_role,
    test_activation_approval_refuses_an_incompatible_existing_actor,
    test_activation_approval_never_overwrites_an_existing_role,
    test_activation_approval_rolls_back_if_the_subject_became_bound,
    test_same_subject_can_be_approved_in_two_scopes_without_fact_leakage,
    test_activation_rejection_writes_only_terminal_and_empty_effect,
    test_activation_decision_revalidates_the_current_admin,
    test_activation_decision_hides_a_request_from_another_scope,
    test_activation_decision_rejects_every_non_current_admin_shape,
    test_terminal_activation_replay_is_a_conflict_without_side_effects,
    test_activation_approval_rolls_back_the_request_with_the_directory,
    test_feishu_admin_can_make_both_activation_decisions,
)

ALL_GROUPS = {"identity_directory": IDENTITY_DIRECTORY_CASES}

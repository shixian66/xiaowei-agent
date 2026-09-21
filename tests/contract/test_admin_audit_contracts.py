"""Admin 审计契约：**结构上写不出一条假的成功**。

审计表是唯一一处既要长期保留、又天然贴着 secret、聊天正文和异常堆栈的存储。它的
防线不是"调用方要小心"，而是三条结构性的窄：

1. 候选事件**没有任何自由文本字段**——留一个 ``str`` 通道，第一个赶工的调用方就会
   把 ``str(exc)`` 塞进去；
2. ``reason_code`` 与 ``outcome``、``effect`` 与 ``action`` 都**双向**绑定，不是单向
   蕴含。只钉一个方向时，"成功却带着原因码"和"改了角色却不记是哪个角色"都能过；
3. 三个窄写类型（start / terminal / denial）各自**缺**着不同的字段，因此没有任何一个
   能独立声称一次目录成功。

规格 §14.2 的十三项字段在这里逐字段相等地钉住：多一个字段和少一个字段都是缺陷。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.admin_audit import (
    DIRECTORY_ACTIONS,
    ROLE_EFFECT_ACTIONS,
    STATUS_EFFECT_ACTIONS,
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEffect,
    AdminAuditEvent,
    AdminAuditStart,
    AdminAuditTerminal,
    AdminOperationContext,
    admin_audit_target_digest,
)

_NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)
_DIGEST = admin_audit_target_digest(
    target_kind=AdminAuditTargetKind.USER, target_ref="u-1"
)

_SPEC_14_2_FIELDS = {
    "event_id",
    "operation_id",
    "tenant_id",
    "environment_id",
    "actor_user_id",
    "actor",
    "auth_source",
    "action",
    "target_kind",
    "target_ref_digest",
    "outcome",
    "reason_code",
    "created_at",
}
"""规格 §14.2 逐行列出的十三项。"""


def _candidate(**overrides: object) -> AdminAuditCandidate:
    base: dict[str, object] = {
        "operation_id": "op-1",
        "tenant_id": "t-1",
        "environment_id": "dev",
        "actor_user_id": "admin-1",
        "actor": "admin",
        "auth_source": IdentitySource.LOCAL_ADMIN,
        "action": AdminAuditAction.ROLE_ASSIGNED,
        "target_kind": AdminAuditTargetKind.USER,
        "target_ref_digest": _DIGEST,
        "outcome": AdminAuditOutcome.SUCCEEDED,
        "effect": AdminAuditEffect(role=ProductRole.OPERATOR),
    }
    return AdminAuditCandidate(**{**base, **overrides})  # type: ignore[arg-type]


def test_operation_context_carries_no_audit_decision() -> None:
    """调用方唯一能提供的东西里，没有任何一项是审计结论。

    动作、目标、结果全部由 store 从命令派生。留一个可选的 ``action`` 参数，就等于
    允许调用方把一次角色撤销记成一次角色授予。
    """
    assert set(AdminOperationContext.model_fields) == {
        "operation_id",
        "actor_user_id",
        "actor",
        "auth_source",
    }


def test_operation_id_leaves_room_for_batch_suffixes() -> None:
    """``operation_id`` ≤ 48，与批量子 id 后缀 ``{op}:{index}`` 相容。

    批量迁移一条一条写审计，子 id 是在 operation_id 后面接序号；上限定成和普通
    ID 一样的 64 时，一个刚好 64 的 operation_id 会让整批在中途炸。
    """
    AdminOperationContext(
        operation_id="o" * 48,
        actor_user_id="admin-1",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
    )
    with pytest.raises(ValidationError):
        AdminOperationContext(
            operation_id="o" * 49,
            actor_user_id="admin-1",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
        )


def test_audit_candidate_fields_are_the_spec_set_plus_the_closed_effect() -> None:
    """候选 = 规格十三项减去 store 盖章的两项，再加一个闭集 effect。"""
    assert set(AdminAuditCandidate.model_fields) == (
        _SPEC_14_2_FIELDS - {"event_id", "created_at"}
    ) | {"effect"}


def test_effect_has_only_two_closed_enum_slots() -> None:
    """effect 是两个闭集枚举槽，不是 JSON——它塞不进正文。

    做成 JSON 或 ``dict`` 就等于在审计表上重新开了一条自由文本通道，而那正是
    不变量 1 要封的东西。
    """
    assert set(AdminAuditEffect.model_fields) == {"role", "status"}
    with pytest.raises(ValidationError):
        AdminAuditEffect(role="whatever")  # type: ignore[arg-type]


def test_stored_event_adds_only_identity_and_time() -> None:
    """事件比候选只多 ``event_id`` 与 ``created_at``，一项不多。"""
    assert set(AdminAuditEvent.model_fields) - set(AdminAuditCandidate.model_fields) == {
        "event_id",
        "created_at",
    }
    assert set(AdminAuditEvent.model_fields) == _SPEC_14_2_FIELDS | {"effect"}


def test_target_kind_is_the_spec_five_member_closed_set() -> None:
    assert {member.value for member in AdminAuditTargetKind} == {
        "user",
        "activation",
        "duty_binding",
        "config",
        "task_content",
    }


def test_outcome_is_the_spec_four_member_closed_set() -> None:
    assert {member.value for member in AdminAuditOutcome} == {
        "started",
        "succeeded",
        "denied",
        "failed",
    }


def test_w1a_actions_cover_only_what_this_stage_actually_writes() -> None:
    """action 闭集不含本阶段写不出的动作。

    提前塞进 ``ACTIVATION_APPROVED`` 这类成员，会让"这个阶段能做什么"在枚举上
    看起来比实际大一圈——而审计枚举正是复审判断范围的地方。
    """
    assert {member.value for member in AdminAuditAction} == {
        "user_created",
        "user_status_changed",
        "role_assigned",
        "role_revoked",
        "external_identity_bound",
        "external_identity_unbound",
        "local_admin_bootstrapped",
        "legacy_identity_migrated",
    }


@pytest.mark.parametrize(
    "name", ["login", "logout", "password", "session", "authenticat"]
)
def test_authentication_lifecycle_never_became_an_audit_action(name: str) -> None:
    """登录/改密/登出不进 Admin 审计（规格 §14.2 明写）。

    它们是认证生命周期事件，不是 Admin 对业务对象的操作；混进来会让审计表同时
    承担两种保留期与两种读者。
    """
    assert not any(name in member.value for member in AdminAuditAction)


def test_target_digest_is_domain_separated_across_kinds() -> None:
    """同一个 ref 在不同 target_kind 下摘要不等。

    用户 id 与任务 id 撞号完全可能。不做域隔离时，两条性质完全不同的事件会算出
    相等的摘要，从此可以互相冒充。
    """
    ref = "shared-1"
    digests = {
        kind: admin_audit_target_digest(target_kind=kind, target_ref=ref)
        for kind in AdminAuditTargetKind
    }
    assert len(set(digests.values())) == len(AdminAuditTargetKind)


def test_target_digest_never_contains_the_plaintext_ref() -> None:
    secret_ref = "ou_" + "0123456789abcdef"
    digest = admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref=secret_ref
    )
    assert secret_ref not in digest
    assert len(digest) == 64


def test_target_ref_digest_field_rejects_a_plaintext_reference() -> None:
    """明文进不了摘要列——形状在入口就被拒。

    只靠"调用方记得先摘要"时，一次忘记会把明文 ``open_id`` 长期留在审计表里，
    而审计表是最不该出现明文主体标识的地方。
    """
    with pytest.raises(ValidationError):
        _candidate(target_ref_digest="u-1")


def test_reason_code_is_absent_on_success_and_required_on_denial() -> None:
    """不变量 2：负面结果必须有原因码，非负面结果必须没有。**双向**。"""
    with pytest.raises(ValidationError):
        _candidate(reason_code=AdminAuditReasonCode.CONFLICT)
    with pytest.raises(ValidationError):
        _candidate(
            outcome=AdminAuditOutcome.DENIED,
            reason_code=None,
            effect=AdminAuditEffect(),
        )
    _candidate(
        outcome=AdminAuditOutcome.DENIED,
        reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
        effect=AdminAuditEffect(),
    )


def test_a_negative_outcome_cannot_claim_an_effect() -> None:
    """不变量 3：没成功却声称 effect，会让读者以为权限已经给出去了。"""
    with pytest.raises(ValidationError):
        _candidate(
            outcome=AdminAuditOutcome.DENIED,
            reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
            effect=AdminAuditEffect(role=ProductRole.ADMIN),
        )


def test_started_outcome_cannot_claim_an_effect_either() -> None:
    """``STARTED`` 同样不能带 effect：结果未知时声称结果，就是伪装成功。"""
    with pytest.raises(ValidationError):
        _candidate(
            action=AdminAuditAction.ROLE_ASSIGNED,
            outcome=AdminAuditOutcome.STARTED,
            effect=AdminAuditEffect(role=ProductRole.ADMIN),
        )


@pytest.mark.parametrize("action", sorted(ROLE_EFFECT_ACTIONS), ids=lambda a: a.value)
def test_role_changing_actions_require_a_role_effect(
    action: AdminAuditAction,
) -> None:
    """改了角色就必须记下是哪个角色，否则后续改动一覆盖就再也还原不出来。"""
    status = (
        UserStatus.ACTIVE if action in STATUS_EFFECT_ACTIONS else None
    )
    _candidate(
        action=action, effect=AdminAuditEffect(role=ProductRole.ADMIN, status=status)
    )
    with pytest.raises(ValidationError):
        _candidate(action=action, effect=AdminAuditEffect(status=status))


@pytest.mark.parametrize("action", sorted(STATUS_EFFECT_ACTIONS), ids=lambda a: a.value)
def test_status_changing_actions_require_a_status_effect(
    action: AdminAuditAction,
) -> None:
    role = ProductRole.ADMIN if action in ROLE_EFFECT_ACTIONS else None
    _candidate(
        action=action, effect=AdminAuditEffect(role=role, status=UserStatus.DISABLED)
    )
    with pytest.raises(ValidationError):
        _candidate(action=action, effect=AdminAuditEffect(role=role))


@pytest.mark.parametrize(
    "action",
    [
        AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
        AdminAuditAction.EXTERNAL_IDENTITY_UNBOUND,
        AdminAuditAction.ROLE_REVOKED,
    ],
    ids=lambda a: a.value,
)
def test_binding_actions_carry_no_effect(action: AdminAuditAction) -> None:
    """绑定/解绑与角色撤销都不产生 effect。

    ``ROLE_REVOKED`` 尤其不能带 role effect：撤销之后该作用域**没有**角色，
    记任何角色都是错的。
    """
    _candidate(action=action, effect=AdminAuditEffect())
    with pytest.raises(ValidationError):
        _candidate(action=action, effect=AdminAuditEffect(role=ProductRole.USER))
    with pytest.raises(ValidationError):
        _candidate(action=action, effect=AdminAuditEffect(status=UserStatus.ACTIVE))


def test_every_w1a_action_is_a_directory_action() -> None:
    """W1a 的八个 action 恰好就是目录动作全集。

    这条同时解释了 ``append_started`` 在本阶段**没有可用 action**：目录动作一律
    单阶段。不要为了让某条用例跑通而放宽那个校验。
    """
    assert DIRECTORY_ACTIONS == frozenset(AdminAuditAction)


def test_two_phase_start_refuses_a_directory_action() -> None:
    """不变量 4：两阶段的 STARTED 在**契约层**就拒绝目录动作。

    只靠数据库 CHECK 不够——内存实现没有数据库，规则放在任一实现里，另一个就会
    在同一个共享套件上给出不同的答案。
    """
    with pytest.raises(ValidationError):
        AdminAuditStart(
            operation_id="op-1",
            tenant_id="t-1",
            environment_id="dev",
            actor_user_id="admin-1",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
            action=AdminAuditAction.ROLE_ASSIGNED,
            target_kind=AdminAuditTargetKind.USER,
            target_ref_digest=_DIGEST,
        )


def test_neither_start_nor_denial_can_state_an_outcome_or_effect() -> None:
    """三个窄写类型在**结构上**写不出一条目录成功。

    这不是"它们碰巧没填"，而是"它们没有那个字段"。通用的
    ``append(candidate)`` 之所以不可接受，正是因为它有。
    """
    forbidden = {"outcome", "effect"}
    assert set(AdminAuditStart.model_fields) & (forbidden | {"reason_code"}) == set()
    assert set(AdminAuditDenial.model_fields) & forbidden == set()
    assert "reason_code" in AdminAuditDenial.model_fields
    assert AdminAuditDenial.model_fields["reason_code"].is_required()
    assert set(AdminAuditTerminal.model_fields) == {
        "operation_id",
        "outcome",
        "reason_code",
        "effect",
    }


def test_candidate_cannot_stamp_event_id_or_time() -> None:
    """不变量 6：``event_id`` 与 ``created_at`` 由 store 盖章。"""
    assert {"event_id", "created_at"} & set(AdminAuditCandidate.model_fields) == set()
    with pytest.raises(ValidationError):
        _candidate(event_id="e-1", created_at=_NOW)

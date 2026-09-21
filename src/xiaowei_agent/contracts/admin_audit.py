"""Admin 审计事件契约：结构上写不出一条假的成功。

**审计事实由 store 从命令派生，调用方不得组装。** 调用方唯一能提供的是
:class:`AdminOperationContext`——operation、操作者、认证来源；``action``、
``target_kind``、``target_ref_digest``、``outcome`` 与 effect 全部由 store 计算。
能被调用方指定，就能被调用方写错，而"校验它有没有写错"永远弱于"它根本没有机会写"。

**本模块不提供任何自由文本字段。** 审计表既要长期保留、又天然贴着 secret、聊天
正文和异常堆栈；只要留一个 ``str`` 通道，第一个赶工的调用方就会把 ``str(exc)``
塞进去，而那时它已经进了备份。原因一律走闭集 ``reason_code``。

三个窄写类型各自**缺**着不同的字段：``AdminAuditStart`` 没有 outcome/effect/原因码，
``AdminAuditDenial`` 没有 outcome/effect 且原因码必填，``AdminAuditTerminal`` 只带
结果三项、稳定字段从已存的 ``STARTED`` 读回。因此没有任何一个能独立声称一次目录
成功——通用的 ``append(candidate)`` 之所以不可接受，正是因为它能。
"""

import hashlib
from typing import Annotated, Final, Self, TypeAlias

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    Sha256Hex,
    StrictStr,
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
from xiaowei_agent.contracts.identity import BoundedId

_OPERATION_ID_MAX_LENGTH: Final[int] = 48
"""调用方提供的**根** operation id 上限，比普通 ID 的 64 更紧。

紧的那一截就是给批量子 id 后缀 ``{op}:{index}`` 留的余量：留成 64 时，一个刚好
64 的 operation_id 会让整批迁移在中途炸，而中途炸掉的批量正是最难还原的那一类失败。
"""

OperationId: TypeAlias = Annotated[
    StrictStr, Field(min_length=1, max_length=_OPERATION_ID_MAX_LENGTH)
]
"""调用方写得出来的那个 id。**只用于** :class:`AdminOperationContext`。"""

AuditOperationId: TypeAlias = BoundedId
"""落库审计事件上的 operation id，走普通 ID 的 64 字符边界。

**它与 :data:`OperationId` 必须是两个类型，不要合并。** 上一版合并了，于是
"给后缀留余量" 只写在注释里、余量却无处可用：一个合法的 48 字符根 id 接上批次
序号后是 54 字符，而审计候选也只接受 48——批量迁移会在生成第一条候选时就校验
失败，事务根本进不去。一个类型同时承担两个角色时，它只能满足其中一个。

不另定一个新上限，而是直接用 :data:`~xiaowei_agent.contracts.identity.BoundedId`：
持久 ID 的边界在本项目只有一个值，再写一遍 64 就是第二份真源。
两个上限的关系（根 + 最大批次序号 ≤ 宽边界）由
``test_a_batch_child_id_derived_from_the_longest_root_still_fits`` 盯住。
"""

_DIGEST_DOMAIN: Final[str] = "xiaowei.admin_audit.target.v1"

DIRECTORY_ACTIONS: Final[frozenset[AdminAuditAction]] = frozenset(AdminAuditAction)
"""同库、单事务的目录动作。

W1a 阶段它**等于全部 action**：本阶段写的每一件事都是同库事实，因此授权改变与
成功审计在同一个事务里提交，不存在"先写 STARTED、再补终态"的中间态。两阶段留给
规格 §14.2 的文件配置操作（W4a），那时会有非目录动作加进来。
"""

ROLE_EFFECT_ACTIONS: Final[frozenset[AdminAuditAction]] = frozenset(
    {
        AdminAuditAction.USER_CREATED,
        AdminAuditAction.ROLE_ASSIGNED,
        AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
        AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
    }
)
"""成功时必须记下"授予了哪个角色"的动作。

``ROLE_REVOKED`` **不在**这里：撤销之后该作用域没有角色，记任何角色都是错的。
"""

STATUS_EFFECT_ACTIONS: Final[frozenset[AdminAuditAction]] = frozenset(
    {
        AdminAuditAction.USER_CREATED,
        AdminAuditAction.USER_STATUS_CHANGED,
        AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
        AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
    }
)
"""成功时必须记下"账号处于哪个状态"的动作。"""

_NEGATIVE_OUTCOMES: Final[frozenset[AdminAuditOutcome]] = frozenset(
    {AdminAuditOutcome.DENIED, AdminAuditOutcome.FAILED}
)


def admin_audit_target_digest(
    *, target_kind: AdminAuditTargetKind, target_ref: str
) -> str:
    """把目标引用算成 domain-separated 摘要。

    ``target_kind`` 进摘要域是必须的：用户 id 与任务 id 撞号完全可能，不隔离就会
    算出相等的摘要，两条性质完全不同的事件从此可以互相冒充。

    版本号进域是为了将来换算法时旧摘要不会与新摘要混在同一个列里被当作相等。
    """
    material = f"{_DIGEST_DOMAIN}\x1f{target_kind.value}\x1f{target_ref}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AdminOperationContext(Contract):
    """一次管理操作里，调用方**唯一**能提供的东西。

    这里没有任何一项是审计结论。留一个可选的 ``action`` 参数，就等于允许调用方
    把一次角色撤销记成一次角色授予。
    """

    operation_id: OperationId
    actor_user_id: BoundedId
    actor: StrictStr = Field(min_length=1, max_length=256)
    auth_source: IdentitySource


class AdminAuditEffect(Contract):
    """成功事件携带的闭集结果，只有两个枚举槽。

    做成 JSON 或 ``dict`` 就等于在审计表上重新开一条自由文本通道。两个槽都可空，
    因为哪些动作该带哪一个由 :class:`AdminAuditCandidate` 的校验按动作决定。
    """

    role: ProductRole | None = None
    status: UserStatus | None = None

    @property
    def is_empty(self) -> bool:
        return self.role is None and self.status is None


class AdminAuditCandidate(Contract):
    """一条**尚未盖章**的审计事件。只由 store 内部构造。

    四条不变量都在这里强制，而不是留给数据库：内存实现没有数据库，规则只放在
    PostgreSQL 一侧时，两个实现会在同一个共享套件上给出不同的答案。数据库那边的
    CHECK 是**第二道**独立表达，不是唯一一道。
    """

    operation_id: AuditOperationId
    tenant_id: BoundedId
    environment_id: BoundedId
    actor_user_id: BoundedId
    actor: StrictStr = Field(min_length=1, max_length=256)
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    target_ref_digest: Sha256Hex
    outcome: AdminAuditOutcome
    reason_code: AdminAuditReasonCode | None = None
    effect: AdminAuditEffect = AdminAuditEffect()

    @model_validator(mode="after")
    def _reason_code_matches_outcome(self) -> Self:
        """不变量 2：负面结果必须有原因码，非负面结果必须没有。**双向**。

        只钉一个方向时，"成功却带着 CONFLICT 原因码"这种事件能进表，而读者无法
        判断它到底成功了没有。
        """
        negative = self.outcome in _NEGATIVE_OUTCOMES
        if negative and self.reason_code is None:
            raise ValueError("a denied or failed event must carry a reason code")
        if not negative and self.reason_code is not None:
            raise ValueError("a non-negative event must not carry a reason code")
        return self

    @model_validator(mode="after")
    def _effect_matches_action_and_outcome(self) -> Self:
        """不变量 3：只有 ``SUCCEEDED`` 能带 effect，且 effect 与 action 双向绑定。

        没成功却声称 effect，会让审计读者以为权限已经给出去了；成功却不记 effect，
        则会在后续改动覆盖当前状态之后，再也还原不出当时授予了什么。
        """
        if self.outcome is not AdminAuditOutcome.SUCCEEDED:
            if not self.effect.is_empty:
                raise ValueError("only a succeeded event may carry an effect")
            return self
        wants_role = self.action in ROLE_EFFECT_ACTIONS
        if wants_role != (self.effect.role is not None):
            raise ValueError("role effect does not match the action")
        wants_status = self.action in STATUS_EFFECT_ACTIONS
        if wants_status != (self.effect.status is not None):
            raise ValueError("status effect does not match the action")
        return self

    @model_validator(mode="after")
    def _directory_actions_are_single_phase(self) -> Self:
        """不变量 4 的另一半：目录动作不得写 ``STARTED``。

        目录动作与它的成功审计在同一个事务里提交，因此不存在"结果未知"的中间态。
        写出一条目录动作的 STARTED，就是在声称有一个本不存在的中间态。
        """
        if (
            self.outcome is AdminAuditOutcome.STARTED
            and self.action in DIRECTORY_ACTIONS
        ):
            raise ValueError("a directory action is single-phase and has no STARTED")
        return self


class AdminAuditEvent(AdminAuditCandidate):
    """已落库的事件：比候选只多 ``event_id`` 与 ``created_at``。

    这两项由 store 盖章。命令自带时间时，两次重放可以写出任意历史时间。
    """

    event_id: BoundedId
    created_at: AwareDatetime


class AdminAuditStart(Contract):
    """两阶段操作的第一条事件。**没有** outcome / effect / 原因码。

    它在**契约层**拒绝目录动作（不变量 4）。W1a 因此没有任何可用的 action——
    ``DIRECTORY_ACTIONS`` 恰好等于本阶段全部八个动作。这是设计结果，不是缺陷：
    ``append_denied`` 是本阶段唯一可用的写方法。**不要**为了让某条用例跑通而放宽
    这条校验——放宽它等于允许目录动作走两阶段，终态字段由调用方再传一遍。
    """

    operation_id: AuditOperationId
    tenant_id: BoundedId
    environment_id: BoundedId
    actor_user_id: BoundedId
    actor: StrictStr = Field(min_length=1, max_length=256)
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    target_ref_digest: Sha256Hex

    @model_validator(mode="after")
    def _refuses_directory_actions(self) -> Self:
        if self.action in DIRECTORY_ACTIONS:
            raise ValueError("a directory action is single-phase and has no STARTED")
        return self


class AdminAuditTerminal(Contract):
    """两阶段操作的终态。稳定字段从已存的 ``STARTED`` 读回，不由调用方再传一遍。

    再传一遍就等于给了调用方一次改写租户、操作者或目标的机会，而那次改写不会与
    任何东西矛盾——第一条事件已经写完了。
    """

    operation_id: AuditOperationId
    outcome: AdminAuditOutcome
    reason_code: AdminAuditReasonCode | None = None
    effect: AdminAuditEffect = AdminAuditEffect()

    @model_validator(mode="after")
    def _is_a_terminal_outcome(self) -> Self:
        if self.outcome is AdminAuditOutcome.STARTED:
            raise ValueError("a terminal event cannot be STARTED")
        if self.outcome is AdminAuditOutcome.DENIED:
            raise ValueError("a denial is written through append_denied")
        negative = self.outcome in _NEGATIVE_OUTCOMES
        if negative and self.reason_code is None:
            raise ValueError("a failed event must carry a reason code")
        if not negative and self.reason_code is not None:
            raise ValueError("a succeeded event must not carry a reason code")
        if negative and not self.effect.is_empty:
            raise ValueError("only a succeeded event may carry an effect")
        return self


class AdminAuditDenial(Contract):
    """一次被拒绝的操作。**没有** outcome / effect，原因码必填。

    ``outcome`` 由构造决定而不是由调用方传：这个类型能写出来的事件只有 ``DENIED``
    一种，因此它在结构上写不出一条成功。
    """

    operation_id: AuditOperationId
    tenant_id: BoundedId
    environment_id: BoundedId
    actor_user_id: BoundedId
    actor: StrictStr = Field(min_length=1, max_length=256)
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    target_ref_digest: Sha256Hex
    reason_code: AdminAuditReasonCode

"""Admin 审计的写边界、阶段定义与盖章。

**这里没有"写一条任意事件"的入口。** 四个窄写方法各自缺着不同的字段，因此没有
任何一个能独立声称一次目录成功；能收下完整候选的函数是**有限的六个**，由
``tests/security/test_identity_write_path.py`` 按签名发现并用 ``==`` 冻结——具体是哪
六个以那份名单为准，**不在这里另记一份数字**：两处各记一份，改的人只会改一处。

错误族与目录侧**分开**：谁的公开方法失败，就用谁的错误族。同一个事实（该
``operation_id`` 在该阶段已有事件），经 :class:`AdminAuditStore` 暴露时是
:class:`AdminAuditConflictError`，经 ``UserDirectoryStore.apply`` 暴露时是一次授权
改变的失败 ``AdminAuditUnwritableError``。规则散在多处时，分叉不会被任何人看见。
"""

import datetime as _dt
import hashlib
from typing import Final, Literal, Protocol, TypeAlias

from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEvent,
    AdminAuditStart,
    AdminAuditTerminal,
)
from xiaowei_agent.contracts.enums import AdminAuditOutcome

AuditStage: TypeAlias = Literal["started", "terminal"]
"""一条事件所处的阶段。

只有两个取值，因为数据库那边只有两条偏唯一索引。内存实现与 PostgreSQL 实现
**共用这一个定义**——各写一份"什么算阶段重复"，共享套件就会在两个实现上给出
不同答案，而那正是套件存在的理由。
"""

_EVENT_ID_DOMAIN: Final[str] = "xiaowei.admin_audit.event.v1"


class AdminAuditError(RuntimeError):
    """审计写入的闭集错误基类。

    是 ``RuntimeError`` 子类，因此 ``classify_persistence_exception`` 对它返回
    ``None``，它能原样穿过 ``_write_transaction`` 与 ``_persistence_boundary``
    两层而不被改写成 ``PersistenceIntegrityError``。
    """


class AdminAuditConflictError(AdminAuditError):
    """该 ``operation_id`` 在该阶段已经有一条事件了。"""

    def __init__(self) -> None:
        super().__init__("an admin audit event already exists at this stage")


class AdminAuditMissingStartError(AdminAuditError):
    """写终态时找不到对应的 ``STARTED``。"""

    def __init__(self) -> None:
        super().__init__("no started event exists for this operation")


def audit_stage_key(
    *, operation_id: str, outcome: AdminAuditOutcome
) -> tuple[str, AuditStage]:
    """一条事件占用的"阶段槽"。

    取 ``operation_id`` 与 ``outcome`` 两个标量而不是取整条候选：写终态前要先按
    ``(operation_id, "started")`` 找回那条 ``STARTED``，而那时手上根本没有候选。
    顺带的好处是它不进"能收下完整候选的函数"那张名单——它确实收不下。
    """
    stage: AuditStage = (
        "started" if outcome is AdminAuditOutcome.STARTED else "terminal"
    )
    return (operation_id, stage)


def seal(candidate: AdminAuditCandidate, *, now: _dt.datetime) -> AdminAuditEvent:
    """给候选盖上 ``event_id`` 与 ``created_at``，得到一条可落库的事件。

    ``event_id`` 由 ``(operation_id, 阶段)`` **确定性**派生，不取随机值：

    1. store 不产生随机源，与 ``LocalAdminStore`` 让调用方传 digest 是同一条纪律；
    2. 重放同一个阶段会算出同一个主键，于是"重放"与"阶段重复"在数据库里撞的是
       同一件事，两个实现不必各自再定义一次什么叫重放。

    摘要取满 64 个十六进制字符，正好是 ``BoundedId`` 的上限。

    逐字段显式构造而不是 ``AdminAuditEvent(**candidate.model_dump(), ...)``：
    ``Contract`` 是 strict 模式，``model_dump()`` 会把 ``effect`` 摊成 ``dict``，
    而 strict 模式下 ``dict`` 不能当作嵌套契约。
    """
    operation_id, stage = audit_stage_key(
        operation_id=candidate.operation_id, outcome=candidate.outcome
    )
    material = f"{_EVENT_ID_DOMAIN}\x1f{operation_id}\x1f{stage}"
    return AdminAuditEvent(
        event_id=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        created_at=now,
        operation_id=candidate.operation_id,
        tenant_id=candidate.tenant_id,
        environment_id=candidate.environment_id,
        actor_user_id=candidate.actor_user_id,
        actor=candidate.actor,
        auth_source=candidate.auth_source,
        action=candidate.action,
        target_kind=candidate.target_kind,
        target_ref_digest=candidate.target_ref_digest,
        outcome=candidate.outcome,
        reason_code=candidate.reason_code,
        effect=candidate.effect,
    )


class AdminAuditStore(Protocol):
    """审计事件的读写边界：三个窄写方法 + 一个按 id 读。

    **没有通用的 ``append(candidate)``**，也没有任何清理、更新或删除 API。审计表
    既要长期保留、又是事后唯一能回答"当时授予了什么"的地方；留一个能写任意候选的
    入口，等于把"派生而非校验"这条纪律退回成一句注释。
    """

    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent:
        """写两阶段操作的第一条事件。

        W1a 没有任何可用的 action：``DIRECTORY_ACTIONS`` 恰好等于本阶段全部八个
        动作，而 :class:`AdminAuditStart` 在契约层就拒绝目录动作。这是设计结果，
        不是缺陷——放宽它等于允许目录动作走两阶段，终态字段由调用方再传一遍。
        """

    async def append_terminal(self, *, terminal: AdminAuditTerminal) -> AdminAuditEvent:
        """写终态；稳定字段从已存的 ``STARTED`` 读回，找不到就抛。"""

    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent:
        """写一次拒绝。本阶段唯一可用的写方法。"""

    async def load(self, *, event_id: str) -> AdminAuditEvent | None:
        """按 id 读回一条事件；不存在返回 ``None``。"""


__all__ = [
    "AdminAuditConflictError",
    "AdminAuditError",
    "AdminAuditMissingStartError",
    "AdminAuditStore",
    "AuditStage",
    "audit_stage_key",
    "seal",
]

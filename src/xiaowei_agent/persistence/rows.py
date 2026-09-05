"""DTO ↔ 数据库行的映射：**纯函数，不碰连接**。

与 ``decisions.py`` 同一个理由分出来：映射写在执行 SQL 的方法里，就只能靠一个真实
数据库才能测——而"往返是否无损"根本不需要数据库来回答。分出来之后，无损性在默认
测试路径上就能证明，集成测试只需要再证"数据库也没弄丢东西"。

**两组独立的往返风险，不能用一组覆盖另一组**：

1. **JSONB 表**：DTO → JSON → DTO。风险在嵌套契约、``FrozenMap``、``tuple`` 与
   ``AlwaysTrue`` 这类特殊字段。
2. **``tasks`` 表**：``TaskRecord`` → 列展开 → ``TaskRecord``。风险完全不同——
   ``AwareDatetime`` 经 ``timestamptz`` 往返的时区与微秒精度、``Sha256Hex`` 的长度
   约束、``terminal_reason`` 的 ``None`` 与 SQL ``NULL`` 的对应，以及三个租约字段的
   同置同清不变量。

**JSON 路径是被契约层要求的，不是实现偏好。** ``Contract`` 全局 ``strict=True``：
python 校验模式下 ``str`` 不能转 ``StrEnum``、ISO 字符串不能转 ``datetime``，而 JSON
校验模式下两者都允许（见 ``contracts/base.py``）。因此从 JSONB 读回的 ``dict`` 必须
经 ``model_validate_json`` 回到契约，不能直接 ``model_validate``。
"""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from xiaowei_agent.contracts import (
    Contract,
    StepOutcomeKind,
    StepResultStatus,
    TaskRecord,
    TaskStatus,
)

if TYPE_CHECKING:
    from xiaowei_agent.persistence.store import StepExecutionRecord

_C = TypeVar("_C", bound=Contract)


def dump_contract(model: Contract) -> dict[str, Any]:
    """契约 → 可写入 JSONB 的普通 ``dict``。

    走 ``model_dump_json`` 再 ``json.loads``，而不是 ``model_dump(mode="json")``：
    前者是 Pydantic 真正的 JSON 序列化路径，与 ``load_contract`` 的 ``validate_json``
    严格互逆。后者在若干边界类型上与 JSON 路径有细微差异，用它会让往返测试证明的
    是另一条路径。
    """
    loaded: dict[str, Any] = json.loads(model.model_dump_json())
    return loaded


def load_contract(model_type: type[_C], payload: Mapping[str, Any]) -> _C:
    """JSONB 读回的 ``dict`` → 契约。

    必须经 JSON 校验模式：strict 下 ``model_validate`` 会拒绝 ISO 字符串形态的
    ``datetime`` 与字符串形态的枚举，而 JSONB 里存的正是这两种形态。
    """
    return model_type.model_validate_json(json.dumps(payload))


def record_to_row(record: TaskRecord) -> dict[str, Any]:
    """``TaskRecord`` → ``tasks`` 表的一行。

    ``terminal_reason`` 与三个租约字段一律**无条件**出现在结果里，取值可以是
    ``None``。省略"当前为 None"的键会让调用方写出"有值才进 SET 子句"的 UPDATE，
    于是清空语义丢失——见 ``decisions.apply_transition`` 的同一条约束。
    """
    return {
        "task_id": record.task_id,
        "tenant_id": record.tenant_id,
        "environment_id": record.environment_id,
        "actor": record.actor,
        "idempotency_key": record.idempotency_key,
        "request_digest": record.request_digest,
        "status": record.status.value,
        "version": record.version,
        "created_seq": record.created_seq,
        "attempt_number": record.attempt_number,
        "task_failure_count": record.task_failure_count,
        "next_attempt_at": record.next_attempt_at,
        "retry_scheduled_by_attempt": record.retry_scheduled_by_attempt,
        "retry_command_digest": record.retry_command_digest,
        "terminal_reason": record.terminal_reason,
        "lease_owner": record.lease_owner,
        "lease_expires_at": record.lease_expires_at,
        "fencing_token": record.fencing_token,
    }


def row_to_record(row: Mapping[str, Any]) -> TaskRecord:
    """``tasks`` 表的一行 → ``TaskRecord``。

    ``status`` 显式转成 ``TaskStatus``：strict 的 python 校验模式不接受裸字符串，
    而这一行的其余字段（``datetime`` / ``int`` / ``str``）本来就是原生类型，走 JSON
    往返反而会引入一次不必要的时间戳字符串化。因此这里**不**复用
    ``load_contract``——两条路径的输入形态不同。
    """
    return TaskRecord(
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        actor=row["actor"],
        idempotency_key=row["idempotency_key"],
        request_digest=row["request_digest"],
        status=TaskStatus(row["status"]),
        version=row["version"],
        created_seq=row["created_seq"],
        attempt_number=row["attempt_number"],
        task_failure_count=row["task_failure_count"],
        next_attempt_at=row["next_attempt_at"],
        retry_scheduled_by_attempt=row["retry_scheduled_by_attempt"],
        retry_command_digest=row["retry_command_digest"],
        terminal_reason=row["terminal_reason"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        fencing_token=row["fencing_token"],
    )


def step_execution_to_row(record: "StepExecutionRecord") -> dict[str, Any]:
    """步骤 journal 契约到列；可空终局字段一律显式保留。"""
    return {
        "task_id": record.task_id,
        "step_id": record.step_id,
        "attempt_count": record.attempt_count,
        "last_fencing_token": record.last_fencing_token,
        "result_status": None
        if record.result_status is None
        else record.result_status.value,
        "kind": None if record.kind is None else record.kind.value,
        "evidence_id": record.evidence_id,
        "commit_digest": record.commit_digest,
        "started_at": record.started_at,
        "committed_at": record.committed_at,
    }


def row_to_step_execution(row: Mapping[Any, Any]) -> "StepExecutionRecord":
    """数据库列到自描述步骤 journal 契约。"""
    from xiaowei_agent.persistence.store import StepExecutionRecord

    status = row["result_status"]
    kind = row["kind"]
    return StepExecutionRecord(
        task_id=row["task_id"],
        step_id=row["step_id"],
        attempt_count=row["attempt_count"],
        last_fencing_token=row["last_fencing_token"],
        result_status=None if status is None else StepResultStatus(status),
        kind=None if kind is None else StepOutcomeKind(kind),
        evidence_id=row["evidence_id"],
        commit_digest=row["commit_digest"],
        started_at=row["started_at"],
        committed_at=row["committed_at"],
    )

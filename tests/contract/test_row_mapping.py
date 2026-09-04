"""DTO ↔ 行的往返必须无损 —— **两组独立风险，不能互相覆盖**。

M4 计划 §10 T3 把往返测试拆成两组，理由是它们暴露的失败完全不同：

1. **JSONB 表**：DTO → JSON → DTO。风险在嵌套契约、``FrozenMap``、``tuple`` 与
   ``AlwaysTrue`` 这类特殊字段——它们在 JSON 里都退化成普通结构，回来时能不能重新
   长成契约要求的形状，不试就不知道。
2. **``tasks`` 表**：``TaskRecord`` → 列展开 → ``TaskRecord``。风险是另一组：
   ``AwareDatetime`` 的时区与微秒、``Sha256Hex`` 的长度约束、``terminal_reason`` 的
   ``None``，以及三个租约字段的同置同清不变量。

**这里证明的是映射无损，不是数据库无损。** 真正的 ``timestamptz`` 精度、JSONB 的键
序与数值表示要到 integration 测试才能证——那一层跑同样的断言，只是中间多一次真实的
写入与读回。把这一层放在默认路径上，是为了让"映射写错了"在没有数据库时就能发现。
"""

import datetime as _dt

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET
from tests.fakes.sinks import make_event

from xiaowei_agent.contracts import (
    ApprovalRequest,
    ApprovalState,
    Contract,
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalSource,
    PipelineStage,
    ResolvedTarget,
    TaskRecord,
    TaskStatus,
    TraceEvent,
)
from xiaowei_agent.persistence.rows import (
    dump_contract,
    load_contract,
    record_to_row,
    row_to_record,
)
from xiaowei_agent.persistence.schema import ALL_TABLES

_NOW = _dt.datetime(2026, 9, 3, 12, 34, 56, 789012, tzinfo=_dt.UTC)

_EVIDENCE = EvidenceEnvelope(
    evidence_id="e1",
    capability_id="starrocks.slow_query.diagnose",
    capability_version="1.0.0",
    facts=({"query_id": "q1", "elapsed_ms": 120},),
    source="starrocks-fake",
    source_kind=ExternalSource.TOOL,
    captured_at=_NOW,
    sampled=False,
    limitations=("only the first page was read",),
    redaction_ref=None,
)

_APPROVAL = ApprovalRequest(
    task_id="t1",
    step_id="s1",
    plan_hash="a" * 64,
    target_fingerprint="b" * 64,
    policy_revision="policy-2026-09-01",
    subject="alice",
    expires_at=_NOW,
    state=ApprovalState.PENDING,
)

_RECORD = TaskRecord(
    task_id="t1",
    tenant_id="dev-local",
    environment_id="dev",
    actor="alice",
    idempotency_key="idem-1",
    request_digest="c" * 64,
    status=TaskStatus.RUNNING,
    version=3,
)


# --- 第一组：JSONB 表 ---------------------------------------------------------


_AUDIT_EVENT = make_event(stage=PipelineStage.ADMISSION, detail={"reason": "denied"})

# 每个 JSONB 列存的是哪个契约。**这张表本身由下面的元测试对着 schema 核**，因为
# 它此前是靠人记得往参数列表里加一项的——``task_audit_events.event`` 就是这样漏掉的：
# 表建了、载荷类型写在 schema 的 docstring 里，往返测试却一次没跑过它。
_JSONB_PAYLOADS: dict[tuple[str, str], Contract] = {
    ("task_plans", "plan"): FIXTURE_PLAN,
    ("task_plans", "target"): FIXTURE_TARGET,
    ("task_evidence", "envelope"): _EVIDENCE,
    ("task_approvals", "request"): _APPROVAL,
    ("task_audit_events", "event"): _AUDIT_EVENT,
}

_EXPECTED_TYPES: dict[tuple[str, str], type[Contract]] = {
    ("task_plans", "plan"): ExecutionPlan,
    ("task_plans", "target"): ResolvedTarget,
    ("task_evidence", "envelope"): EvidenceEnvelope,
    ("task_approvals", "request"): ApprovalRequest,
    ("task_audit_events", "event"): TraceEvent,
}


def test_every_jsonb_column_has_a_round_trip_fixture() -> None:
    """新增一个 JSONB 列却忘了给它样本时，往返测试照常全绿——覆盖缺口不可见。

    因此覆盖完整性由 schema 决定，不由参数列表决定：从 ``ALL_TABLES`` 里把 JSONB
    列扫出来，与登记表比对。理由与 ``test_suite_bindings.py`` 用 AST 扫绑定相同。
    """
    declared = {
        (table.name, column.name)
        for table in ALL_TABLES
        for column in table.columns
        if isinstance(column.type, JSONB)
    }
    assert declared == set(_JSONB_PAYLOADS)
    assert declared == set(_EXPECTED_TYPES)


@pytest.mark.parametrize(
    ("column", "model"),
    list(_JSONB_PAYLOADS.items()),
    ids=[f"{table}.{column}" for table, column in _JSONB_PAYLOADS],
)
def test_jsonb_payload_round_trips(column: tuple[str, str], model: Contract) -> None:
    """样本的类型也要对：登记表里放错契约，往返照样通过。"""
    assert type(model) is _EXPECTED_TYPES[column]
    assert load_contract(type(model), dump_contract(model)) == model


def test_dumped_payload_is_plain_json_types() -> None:
    """写进 JSONB 的必须是普通 JSON 类型。

    ``FrozenMap`` 是 ``MappingProxyType``，``tuple`` 不是 ``list``——任何一个漏掉
    序列化，驱动会在写入时才报错，而那时错误信息指向的是连接层，不是这里。
    """
    payload = dump_contract(_EVIDENCE)

    def _assert_plain(value: object) -> None:
        assert type(value) in (dict, list, str, int, float, bool, type(None)), type(value)
        if isinstance(value, dict):
            for key, item in value.items():
                assert type(key) is str
                _assert_plain(item)
        elif isinstance(value, list):
            for item in value:
                _assert_plain(item)

    _assert_plain(payload)


def test_load_rejects_a_payload_that_lost_a_field() -> None:
    """反例：往返测试若只走"存了什么读回什么"，删字段也会通过。"""
    payload = dump_contract(_EVIDENCE)
    del payload["capability_id"]
    with pytest.raises(ValueError, match="validation error"):
        load_contract(EvidenceEnvelope, payload)


# --- 第二组：``tasks`` 表的列展开 ---------------------------------------------


def test_task_record_round_trips_through_columns() -> None:
    assert row_to_record(record_to_row(_RECORD)) == _RECORD


def test_leased_task_record_round_trips_through_columns() -> None:
    """带租约的记录是另一条路径：三个字段同时非空，且含 ``AwareDatetime``。"""
    leased = _RECORD.model_copy(
        update={
            "lease_owner": "worker-1",
            "lease_expires_at": _NOW,
            "fencing_token": 7,
        }
    )
    restored = row_to_record(record_to_row(leased))
    assert restored == leased
    assert restored.lease_expires_at == _NOW
    assert restored.lease_expires_at.tzinfo is not None
    assert restored.lease_expires_at.microsecond == _NOW.microsecond


def test_row_keeps_none_columns_explicitly() -> None:
    """``None`` 必须**出现在**行里，而不是被省略。

    省略"当前为 None"的键会让调用方写出"有值才进 SET 子句"的 UPDATE，于是清空语义
    丢失——与 ``decisions.apply_transition`` 挡的是同一个错误，只是换了一层。
    """
    row = record_to_row(_RECORD)
    for column in ("terminal_reason", "lease_owner", "lease_expires_at", "fencing_token"):
        assert column in row
        assert row[column] is None


def test_status_is_stored_as_its_string_value() -> None:
    """枚举必须以字符串值落库；存成 ``repr`` 会让 SQL 里的状态过滤全部失配。"""
    assert record_to_row(_RECORD)["status"] == "running"


def test_row_to_record_rejects_a_half_set_lease() -> None:
    """反例：数据库层的 CHECK 与契约层的校验器是**两层独立表达**。

    读回路径必须自己也拒绝半置位的租约，而不是假定"能存进去的就一定合法"——迁移
    可以被手工绕过，旧数据也可能来自尚未加 CHECK 的版本。
    """
    row = record_to_row(_RECORD)
    row["lease_owner"] = "worker-1"
    with pytest.raises(ValueError, match="validation error"):
        row_to_record(row)


def test_row_to_record_rejects_a_naive_timestamp() -> None:
    """naive datetime 没有确定的时刻，租约过期判定会随进程时区漂移。"""
    row = record_to_row(_RECORD)
    row |= {
        "lease_owner": "worker-1",
        "lease_expires_at": _NOW.replace(tzinfo=None),
        "fencing_token": 7,
    }
    with pytest.raises(ValueError, match="validation error"):
        row_to_record(row)

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
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

from xiaowei_agent.contracts import (
    ApprovalRequest,
    ApprovalState,
    Contract,
    EvidenceEnvelope,
    ExternalSource,
    TaskRecord,
    TaskStatus,
)
from xiaowei_agent.persistence.rows import (
    dump_contract,
    load_contract,
    record_to_row,
    row_to_record,
)

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


@pytest.mark.parametrize(
    "model",
    [FIXTURE_PLAN, FIXTURE_TARGET, _EVIDENCE, _APPROVAL],
    ids=["plan", "target", "evidence", "approval"],
)
def test_jsonb_payload_round_trips(model: Contract) -> None:
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

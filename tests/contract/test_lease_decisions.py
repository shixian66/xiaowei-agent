"""``decisions.py`` 里几条**无法在存储层确定性验证**的判定。

存储层用例是首选：它同时约束两个实现。但有些性质在存储层只能概率性地被证伪——
``list_stale_leases`` 的 ``task_id`` 决胜位就是一例：``task_id`` 由 ``uuid4`` 生成，
插入顺序有一半概率**恰好已是**字典序，于是"排序键第二项被改成常量"这个变异有一半
概率不被发现。一个一半概率转红的反证等于没有反证。

因此这类性质在纯函数层用构造好的输入确定性地钉死，存储层保留契约断言。两层各司其
职，不是重复。
"""

import datetime as _dt

import pytest

from xiaowei_agent.contracts import TaskRecord, TaskStatus
from xiaowei_agent.persistence.decisions import is_stale_lease, stale_lease_sort_key

_EXPIRY = _dt.datetime(2026, 9, 3, 10, 0, 0, tzinfo=_dt.UTC)
_LATER = _EXPIRY + _dt.timedelta(seconds=1)


def _leased(task_id: str, *, expires_at: _dt.datetime, status: TaskStatus) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        idempotency_key=f"idem-{task_id}",
        request_digest="a" * 64,
        status=status,
        version=1,
        lease_owner="w1",
        lease_expires_at=expires_at,
        fencing_token=1,
    )


def test_sort_key_breaks_ties_by_task_id() -> None:
    """同刻过期时按 ``task_id`` 决胜——这是全序的唯一来源。

    输入刻意构造成"插入顺序与字典序相反"，因此稳定排序帮不上忙：决胜位一旦失效，
    这条必定转红。
    """
    later_id = _leased("b", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    earlier_id = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    ordered = sorted([later_id, earlier_id], key=stale_lease_sort_key)
    assert [record.task_id for record in ordered] == ["a", "b"]


def test_sort_key_puts_the_oldest_expiry_first() -> None:
    """过期最久的排最前；时间优先于 ``task_id``。"""
    fresh = _leased("a", expires_at=_LATER, status=TaskStatus.RUNNING)
    old = _leased("z", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    ordered = sorted([fresh, old], key=stale_lease_sort_key)
    assert [record.task_id for record in ordered] == ["z", "a"]


def test_sort_key_rejects_a_record_that_was_never_leased() -> None:
    """反例：编造一个时间戳会让它悄悄排到最前面，看起来像"最该处理的任务"。"""
    never = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING).model_copy(
        update={"lease_owner": None, "lease_expires_at": None, "fencing_token": None}
    )
    with pytest.raises(ValueError, match="ever leased"):
        stale_lease_sort_key(never)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(TaskStatus.RUNNING, True), (TaskStatus.SUCCEEDED, False), (TaskStatus.FAILED, False)],
    ids=lambda value: getattr(value, "value", str(value)),
)
def test_is_stale_lease_excludes_terminal_statuses(
    status: TaskStatus, expected: bool
) -> None:
    """终态必须单独挡：租约三字段在过期后**不清空**，否则每个正常结束的任务都会被
    永久列为"可恢复"。"""
    record = _leased("a", expires_at=_EXPIRY, status=status)
    assert is_stale_lease(record, now=_LATER) is expected


def test_is_stale_lease_uses_a_strict_expiry_comparison() -> None:
    """恰好等于过期时刻的租约**已经**过期。

    ``lease_is_live`` 用的是 ``lease_expires_at > now``，边界归"已过期"一侧。这条
    钉住边界方向：翻成 ``>=`` 会让一个刚过期的租约在恰好那一刻仍被认为有效，而
    抢占逻辑正好在这个时刻发生。
    """
    record = _leased("a", expires_at=_EXPIRY, status=TaskStatus.RUNNING)
    assert is_stale_lease(record, now=_EXPIRY) is True
    assert is_stale_lease(record, now=_EXPIRY - _dt.timedelta(microseconds=1)) is False

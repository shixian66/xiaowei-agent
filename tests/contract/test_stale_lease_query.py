"""`list_stale_leases` 的 SQL 谓词必须与 `is_stale_lease` **逐条等价**，不只是更宽。

原先的规则是「SQL 谓词只被允许更宽，收窄会让两个实现给出不同的结果集」。**那条规则
在有 ``LIMIT`` 的前提下不成立**：更宽的谓词会让 ``LIMIT`` 被将要被丢弃的行吃掉，于是
真正需要恢复的任务被挤出窗口。而 ``apply_transition`` 有意不清租约字段，因此每一个
正常结束的任务都永久命中"曾被租出 + 已过期"并排在最前——返回量会随系统运行单调衰减
到零。内存实现是"先过滤、再排序、再截断"，两个实现就此分叉。

行为反证在共享套件里（``test_a_terminal_task_cannot_crowd_out_a_stale_one``），但它绑到
PostgreSQL 的那一半**只在 integration 里跑**。本文件在默认路径上离线编译同一条语句，
把"有 LIMIT 就必须已经排除终态"这条不变量钉死，因此没有数据库也能立刻转红。
"""

import datetime as _dt

import pytest
from sqlalchemy.dialects import postgresql

from xiaowei_agent.contracts import TERMINAL_STATUSES, TaskStatus
from xiaowei_agent.persistence.postgres import stale_lease_statement

_NOW = _dt.datetime(2026, 9, 4, 12, 0, tzinfo=_dt.UTC)


def _compiled(*, limit: int = 5) -> str:
    return str(
        stale_lease_statement(now=_NOW, limit=limit).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_a_limited_query_must_already_exclude_every_terminal_status() -> None:
    """这就是那条不变量本身：谓词收不干净就不许 ``LIMIT``。

    逐个终态断言，而不是"有 NOT IN 就算过"——漏掉其中一个，剩下的那种终态照样能
    占满窗口。
    """
    sql = _compiled()
    assert "LIMIT" in sql, "语句没有 LIMIT，这条检查就没有对象"
    for status in TERMINAL_STATUSES:
        assert f"'{status.value}'" in sql, f"终态 {status.value} 没有被 SQL 排除"


def test_the_terminal_set_is_derived_not_hardcoded() -> None:
    """反空洞：终态集合若被写死，新增一个终态时上一条会平凡通过。

    ``TERMINAL_STATUSES`` 现有五个成员；这里断言 SQL 里出现的终态字面量数量与它一致，
    且**非终态一个都不许出现**——把 ``running`` 之类误列进去会让正在跑的任务被当成
    不可恢复。
    """
    sql = _compiled()
    quoted = {f"'{status.value}'" for status in TaskStatus}
    present = {literal for literal in quoted if literal in sql}
    assert present == {f"'{status.value}'" for status in TERMINAL_STATUSES}


def test_the_lease_predicate_does_not_narrow_on_null_expiry() -> None:
    """``expires <= now`` 在 ``expires IS NULL`` 时求值为 NULL 而非 TRUE，会**收窄**谓词。

    ``is_stale_lease`` 用的是 ``not lease_is_live``，而 ``lease_is_live`` 要求三个字段
    同时有值。因此正确的 SQL 是对整个合取取反，不是简写成一个比较。
    """
    sql = _compiled()
    assert "NOT (tasks.lease_owner IS NOT NULL" in sql
    assert "tasks.lease_expires_at IS NOT NULL" in sql
    assert "tasks.lease_expires_at <= " not in sql, (
        "简写成 expires <= now 会在 expires IS NULL 时收窄谓词"
    )


def test_ordering_is_total_and_matches_the_shared_sort_key() -> None:
    """SQL 的排序必须与 ``stale_lease_sort_key`` 同序，否则 ``LIMIT`` 截出来的前 N 条
    与权威判定认为的前 N 条不是同一批。"""
    assert "ORDER BY tasks.lease_expires_at, tasks.task_id" in _compiled()


@pytest.mark.parametrize("limit", [1, 5, 100])
def test_the_limit_reaches_the_statement(limit: int) -> None:
    """反空洞：limit 若没被真的用上，第一条里的 ``LIMIT`` 断言会变成一句空话。"""
    assert f"LIMIT {limit}" in _compiled(limit=limit)

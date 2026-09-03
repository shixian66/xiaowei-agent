"""租约与 fencing 的闭合规则。

只在"调用方传了 token"时才校验，等于不传即绕过。这里把五种组合全部钉死。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import TaskStatus, TransitionRejection

pytestmark = pytest.mark.security


async def test_second_holder_is_refused_while_lease_is_live(store, task) -> None:
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30) is None


async def test_expired_lease_can_be_taken_over_with_a_higher_token(
    store, task, clock
) -> None:
    first = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    second = await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    assert second is not None
    assert second.fencing_token > first.fencing_token


async def test_renew_keeps_the_same_token(store, task) -> None:
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    renewed = await store.renew_lease(
        task_id=task.task_id,
        owner="w1",
        fencing_token=granted.fencing_token,
        ttl_seconds=30,
    )
    assert renewed is not None
    assert renewed.fencing_token == granted.fencing_token


async def test_renew_by_a_non_owner_is_refused(store, task) -> None:
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert (
        await store.renew_lease(
            task_id=task.task_id,
            owner="w2",
            fencing_token=granted.fencing_token,
            ttl_seconds=30,
        )
        is None
    )


async def test_renew_with_a_wrong_token_is_refused(store, task) -> None:
    """续租必须同时匹配 owner **与** fencing token。

    T1 变异反证发现的覆盖缺口：把 ``may_renew_lease`` 改成只比 owner，全部 1236 条
    用例仍然全绿。只比 owner 的后果是同名 worker 的**旧进程**可以续上**新进程**的
    租约——owner 名字通常是主机名或角色名，进程重启后重名是常态，而那正是 fencing
    token 存在的理由。既有用例只覆盖了"换个 owner 续租被拒"，没有覆盖"同 owner、
    错 token"。
    """
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert (
        await store.renew_lease(
            task_id=task.task_id,
            owner="w1",
            fencing_token=granted.fencing_token + 1,
            ttl_seconds=30,
        )
        is None
    )


async def test_renew_after_expiry_is_refused(store, task, clock) -> None:
    """过期后必须重新 acquire，否则旧 worker 可以复活一个已被抢占的租约。"""
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    assert (
        await store.renew_lease(
            task_id=task.task_id,
            owner="w1",
            fencing_token=granted.fencing_token,
            ttl_seconds=30,
        )
        is None
    )


async def test_stale_token_write_is_rejected(store, task, clock) -> None:
    old = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    current = await store.get(task.task_id)
    result = await store.transition(
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=old.fencing_token,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.STALE_FENCING_TOKEN


async def test_write_without_token_under_live_lease_is_rejected(store, task) -> None:
    """不传 token 即跳过 fencing 校验，等于没有 fencing。"""
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    current = await store.get(task.task_id)
    result = await store.transition(
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_token_without_any_live_lease_is_rejected(store, task) -> None:
    """持有一个不存在的租约同样是违规。"""
    result = await store.transition(
        task_id=task.task_id,
        expected_version=task.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=1,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_transition_without_any_lease_is_allowed(store, task) -> None:
    """created → planning 发生在取租约之前，必须放行，否则任务无法启动。"""
    result = await store.transition(
        task_id=task.task_id,
        expected_version=task.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is True


async def test_terminal_task_cannot_acquire_lease(store, task) -> None:
    """已终态的任务不应再被任何 worker 领走。"""
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED
    )
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30) is None


async def test_stale_token_is_reported_before_version_mismatch(store, task, clock) -> None:
    """两者同时违反时，报出的必须是更具体的 fencing 原因。"""
    old = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    result = await store.transition(
        task_id=task.task_id,
        expected_version=999,
        to_status=TaskStatus.PLANNING,
        fencing_token=old.fencing_token,
    )
    assert result.rejection is TransitionRejection.STALE_FENCING_TOKEN


async def test_expired_lease_cannot_be_bypassed_by_omitting_the_token(
    store, task, clock
) -> None:
    """租约过期后，stale worker 不带 token 也不得写入。

    把规则锚定在"租约此刻是否 live"会留下这个缺口：过期后 live 为假，"有租约缺
    token"的分支就不再命中，省略 token 反而畅通。正确锚点是"任务是否曾被租出"。
    """
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    current = await store.get(task.task_id)
    result = await store.transition(
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_expired_lease_cannot_be_used_with_its_old_token_either(
    store, task, clock
) -> None:
    """过期后必须重新 acquire，带着旧 token 同样不行。"""
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    current = await store.get(task.task_id)
    result = await store.transition(
        task_id=task.task_id,
        expected_version=current.version,
        to_status=TaskStatus.PLANNING,
        fencing_token=granted.fencing_token,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


# --- public 入参的隐式转换（Protocol 标注在运行时不拦任何东西） ---
async def test_expected_version_rejects_bool(store, task) -> None:
    """False 会匹配版本 0，True 会匹配版本 1。"""
    with pytest.raises(ValidationError):
        await store.transition(
            task_id=task.task_id, expected_version=False, to_status=TaskStatus.PLANNING
        )


async def test_fencing_token_rejects_bool(store, task) -> None:
    """True 会匹配 token 1。"""
    with pytest.raises(ValidationError):
        await store.transition(
            task_id=task.task_id,
            expected_version=task.version,
            to_status=TaskStatus.PLANNING,
            fencing_token=True,
        )


async def test_to_status_rejects_a_plain_string(store, task) -> None:
    with pytest.raises(ValidationError):
        await store.transition(
            task_id=task.task_id, expected_version=task.version, to_status="planning"
        )


@pytest.mark.parametrize("bad_ttl", [0, -10])
async def test_acquire_lease_rejects_non_positive_ttl(store, task, bad_ttl: int) -> None:
    with pytest.raises(ValidationError):
        await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=bad_ttl)


async def test_acquire_lease_rejects_bytes_owner(store, task) -> None:
    with pytest.raises(ValidationError):
        await store.acquire_lease(task_id=task.task_id, owner=b"w1", ttl_seconds=30)


@pytest.mark.parametrize(
    ("owner", "ttl"), [(b"w1", 30), ("w1", -10), ("", 30)], ids=["bytes", "ttl", "blank"]
)
async def test_invalid_lease_arguments_do_not_mutate_the_store(
    store, task, owner: object, ttl: int
) -> None:
    """非法入参必须在**写入之前**失败。

    先污染 store 再抛 ValidationError 会留下一个带着无效租约字段的任务记录，
    之后所有 fencing 判定都建立在这条脏记录上。
    """
    before = await store.get(task.task_id)
    with pytest.raises(ValidationError):
        await store.acquire_lease(task_id=task.task_id, owner=owner, ttl_seconds=ttl)
    after = await store.get(task.task_id)
    assert after == before

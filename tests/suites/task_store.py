"""``TaskStore`` 的行为用例：**一份定义，多处绑定**。

M4 要让 ``InMemoryTaskStore`` 与 ``PostgresTaskStore`` 通过同一套行为断言。此前这些
用例散在三个文件里、直接依赖 ``store`` fixture 指向内存实现；要让 PostgreSQL 也跑
一遍，有两条路：

1. **把 ``store`` fixture 参数化成"内存 + PostgreSQL"。** 不行——这三个文件里有两个
   位于 ``tests/security/``，而 socket 放行只对 ``tests/integration/`` 生效。参数化会
   让 ``tests/security/`` 的每条用例都尝试连库，与"安全测试不碰网络"直接冲突。
2. **把用例抽出来，由绑定方提供 ``store``。** 本模块走这条。

本模块**不被 pytest 收集**（文件名不匹配 ``test_*.py``）。用例函数在这里定义，由各
绑定模块调用 ``bind()`` 挂进自己的命名空间；``store`` / ``clock`` / ``task`` 三个
fixture 由绑定方所在目录的 conftest 提供。

搬运是**纯搬运**：一行行为断言都没改。这些用例本来就写得实现无关——断言的是
``second.fencing_token > first.fencing_token``，不是某个具体取值。

**新增用例必须同时加进下面某个分组**，否则它在所有绑定里都收不到，看起来像"写了
测试"，实际一次都没跑。这条由 ``tests/contract/test_task_store_bindings.py`` 承重。
"""

from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import drive_to_terminal, make_envelope

from xiaowei_agent.contracts import TERMINAL_STATUSES, TaskStatus, TransitionRejection
from xiaowei_agent.persistence import (
    ContextMismatchError,
    IdempotencyConflictError,
    TaskNotFoundError,
)


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    """把共享用例挂进绑定模块的命名空间，供 pytest 按普通模块级函数收集。

    绑定模块自己的 ``pytestmark`` 照常生效：marker 在收集期按模块读取，不写回函数
    对象，因此同一个函数可以在 ``tests/security/`` 带 ``security`` marker、在
    ``tests/contract/`` 不带，互不影响。
    """
    for case in cases:
        namespace[case.__name__] = case


# --- 幂等作用域与创建 ---------------------------------------------------------


async def test_create_is_idempotent_by_key(store, context) -> None:
    first = await store.create_task(envelope=make_envelope(), context=context)
    second = await store.create_task(envelope=make_envelope(), context=context)
    assert first.task_id == second.task_id
    assert first.version == second.version


async def test_retry_with_a_new_request_id_reuses_the_same_task(store, context) -> None:
    """同键同语义的正常重试必须复用任务。

    request_id 每次重试都不同，若把它算进去重摘要，合法重试会被误判为"同键不同
    请求"而被拒。
    """
    first = await store.create_task(envelope=make_envelope(request_id="r1"), context=context)
    second = await store.create_task(envelope=make_envelope(request_id="r2"), context=context)
    assert first.task_id == second.task_id
    assert first.version == second.version


async def test_idempotency_key_is_scoped_per_tenant(store, context) -> None:
    """全局键表会让跨租户同键共用一个任务。"""
    a = await store.create_task(envelope=make_envelope(), context=context)
    other = context.model_copy(update={"tenant_id": "other-tenant"})
    b = await store.create_task(
        envelope=make_envelope(tenant_id="other-tenant"), context=other
    )
    assert a.task_id != b.task_id


async def test_idempotency_key_is_scoped_per_environment(store, context) -> None:
    a = await store.create_task(envelope=make_envelope(), context=context)
    other = context.model_copy(update={"environment_id": "staging"})
    b = await store.create_task(
        envelope=make_envelope(environment_id="staging"), context=other
    )
    assert a.task_id != b.task_id


async def test_same_key_with_a_different_request_is_rejected(store, context) -> None:
    """否则第二个不同的请求会静默搭上第一个任务的结果。"""
    await store.create_task(envelope=make_envelope(), context=context)
    with pytest.raises(IdempotencyConflictError):
        await store.create_task(
            envelope=make_envelope(text="a completely different question"), context=context
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("tenant_id", "other-tenant"), ("actor", "mallory"), ("environment_id", "prod")],
)
async def test_envelope_context_mismatch_is_rejected(store, context, field, value) -> None:
    """伪造信封上下文必须拒绝，否则任务会记在未经解析确认的租户/环境下。"""
    with pytest.raises(ContextMismatchError):
        await store.create_task(envelope=make_envelope(**{field: value}), context=context)


async def test_envelope_without_environment_id_is_accepted(store, context) -> None:
    """environment_id 在信封中可选；缺省时以 context 解析结果为准（ADR-007 D2）。"""
    record = await store.create_task(
        envelope=make_envelope(environment_id=None), context=context
    )
    assert record.environment_id == context.environment_id


async def test_get_unknown_task_raises(store) -> None:
    with pytest.raises(TaskNotFoundError):
        await store.get("no-such-task")


# --- CAS 与迁移合法性 ---------------------------------------------------------


async def test_cas_success_bumps_version(store, task) -> None:
    result = await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.PLANNING
    )
    assert result.applied is True
    assert result.rejection is None
    assert result.winner.version == task.version + 1


async def test_cas_failure_returns_the_storage_winner(store, task) -> None:
    """调用方必须能拿到 winner，而不是只知道"失败了"。"""
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.PLANNING
    )
    stale = await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.RUNNING
    )
    assert stale.applied is False
    assert stale.rejection is TransitionRejection.VERSION_MISMATCH
    assert stale.winner.status is TaskStatus.PLANNING
    assert stale.winner.version == task.version + 1


async def test_illegal_transition_is_rejected(store, task) -> None:
    result = await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.SUCCEEDED
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.ILLEGAL_TRANSITION


async def test_terminal_reason_is_cleared_when_a_transition_omits_it(store, task) -> None:
    """``terminal_reason`` **无条件写入，包括写 ``None``**。

    T1 变异反证发现的覆盖缺口：把 ``apply_transition`` 改成"有值才写"，全部 1236 条
    用例仍然全绿。那个改法看起来无害，实际会让"先失败置上原因、再重试转为非终态"
    的记录残留旧原因——调用方拿到一条状态与原因互相矛盾的记录，而没有任何断言拦它。

    这条同时是 M4 的跨实现约束：PostgreSQL 实现必须同样无条件 ``SET``，包括
    ``SET terminal_reason = NULL``，不得写成"有值才进 SET 子句"。
    """
    marked = await store.transition(
        task_id=task.task_id,
        expected_version=task.version,
        to_status=TaskStatus.PLANNING,
        terminal_reason="transient.upstream_timeout",
    )
    assert marked.applied
    assert marked.winner.terminal_reason == "transient.upstream_timeout"

    resumed = await store.transition(
        task_id=task.task_id,
        expected_version=marked.winner.version,
        to_status=TaskStatus.RUNNING,
    )
    assert resumed.applied
    assert resumed.winner.terminal_reason is None
    assert (await store.get(task.task_id)).terminal_reason is None


# --- 租约与 fencing -----------------------------------------------------------


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


# --- public 入参的隐式转换（Protocol 标注在运行时不拦任何东西） ---------------


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


# --- 终态保护 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value), ids=lambda s: s.value
)
async def test_no_transition_out_of_any_terminal_status(store, task, terminal) -> None:
    await drive_to_terminal(store, task.task_id, terminal)
    current = await store.get(task.task_id)
    for target in TaskStatus:
        result = await store.transition(
            task_id=task.task_id, expected_version=current.version, to_status=target
        )
        assert result.applied is False
        assert result.rejection is TransitionRejection.TERMINAL_PROTECTED
        assert result.winner.status is terminal


async def test_terminal_protection_wins_over_version_mismatch(store, task) -> None:
    """拒绝原因必须指向最具体的违规，否则真实原因会被掩盖。"""
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED
    )
    result = await store.transition(
        task_id=task.task_id, expected_version=999, to_status=TaskStatus.RUNNING
    )
    assert result.rejection is TransitionRejection.TERMINAL_PROTECTED


# --- 分组 ---------------------------------------------------------------------
#
# 分组决定用例落在哪个绑定模块，从而决定它带不带 ``security`` marker。分组之间不得
# 重叠，并集必须覆盖本模块定义的全部用例——两条都由 test_task_store_bindings.py
# 承重，因为"漏进分组"的用例在所有绑定里都收不到，看起来像写了测试，实际一次没跑。

CONTRACT_CASES = (
    test_create_is_idempotent_by_key,
    test_retry_with_a_new_request_id_reuses_the_same_task,
    test_idempotency_key_is_scoped_per_tenant,
    test_idempotency_key_is_scoped_per_environment,
    test_same_key_with_a_different_request_is_rejected,
    test_envelope_context_mismatch_is_rejected,
    test_envelope_without_environment_id_is_accepted,
    test_get_unknown_task_raises,
    test_cas_success_bumps_version,
    test_cas_failure_returns_the_storage_winner,
    test_illegal_transition_is_rejected,
    test_terminal_reason_is_cleared_when_a_transition_omits_it,
)

LEASE_FENCING_CASES = (
    test_second_holder_is_refused_while_lease_is_live,
    test_expired_lease_can_be_taken_over_with_a_higher_token,
    test_renew_keeps_the_same_token,
    test_renew_by_a_non_owner_is_refused,
    test_renew_with_a_wrong_token_is_refused,
    test_renew_after_expiry_is_refused,
    test_stale_token_write_is_rejected,
    test_write_without_token_under_live_lease_is_rejected,
    test_token_without_any_live_lease_is_rejected,
    test_transition_without_any_lease_is_allowed,
    test_terminal_task_cannot_acquire_lease,
    test_stale_token_is_reported_before_version_mismatch,
    test_expired_lease_cannot_be_bypassed_by_omitting_the_token,
    test_expired_lease_cannot_be_used_with_its_old_token_either,
    test_expected_version_rejects_bool,
    test_fencing_token_rejects_bool,
    test_to_status_rejects_a_plain_string,
    test_acquire_lease_rejects_non_positive_ttl,
    test_acquire_lease_rejects_bytes_owner,
    test_invalid_lease_arguments_do_not_mutate_the_store,
)

TERMINAL_PROTECTION_CASES = (
    test_no_transition_out_of_any_terminal_status,
    test_terminal_protection_wins_over_version_mismatch,
)

ALL_GROUPS = {
    "contract": CONTRACT_CASES,
    "lease_fencing": LEASE_FENCING_CASES,
    "terminal_protection": TERMINAL_PROTECTION_CASES,
}

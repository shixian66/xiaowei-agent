"""M3/M4 共用的 TaskStore 交互形状。M4 的 PostgreSQL 实现必须原样通过本文件。"""

import pytest
from tests.conftest import make_envelope

from xiaowei_agent.contracts import TaskStatus, TransitionRejection
from xiaowei_agent.persistence import (
    ContextMismatchError,
    IdempotencyConflictError,
    TaskNotFoundError,
)


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

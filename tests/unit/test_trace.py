"""trace_id 的生成、绑定与并发隔离。"""

import asyncio
import re

import pytest

from xiaowei_agent.trace import bind_trace_id, get_trace_id, new_trace_id

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")


def test_new_trace_id_shape_and_uniqueness() -> None:
    a, b = new_trace_id(), new_trace_id()
    assert _HEX32.match(a) and _HEX32.match(b)
    assert a != b


def test_new_trace_id_does_not_bind() -> None:
    new_trace_id()
    assert get_trace_id() is None


def test_bind_and_restore() -> None:
    assert get_trace_id() is None
    with bind_trace_id() as outer:
        assert get_trace_id() == outer
        with bind_trace_id() as inner:
            assert inner != outer
            assert get_trace_id() == inner
        assert get_trace_id() == outer
    assert get_trace_id() is None


def test_bind_explicit_value() -> None:
    with bind_trace_id("a" * 32) as t:
        assert t == "a" * 32 == get_trace_id()


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 32, "a" * 31, "a" * 33, "-" * 32])
def test_bind_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        with bind_trace_id(bad):
            pass


def test_restores_on_exception() -> None:
    with pytest.raises(RuntimeError), bind_trace_id():
        raise RuntimeError("boom")
    assert get_trace_id() is None


async def test_concurrent_tasks_do_not_share_trace_id() -> None:
    async def worker(tag: str) -> tuple[str, str | None]:
        with bind_trace_id() as t:
            await asyncio.sleep(0)
            return tag, get_trace_id() if get_trace_id() == t else None

    results = await asyncio.gather(worker("a"), worker("b"))
    ids = [r[1] for r in results]
    assert all(ids), "每个任务退出时必须仍看到自己绑定的 trace_id"
    assert ids[0] != ids[1]
    assert get_trace_id() is None


def test_trace_id_format_has_a_single_definition() -> None:
    """生成端与校验端共用同一个格式常量，否则两处会悄悄漂移。

    contracts 侧用 ``re.fullmatch``（pydantic v2 的 Rust regex 引擎不支持
    ``\\A`` / ``\\Z``），trace 侧用显式锚点；两者必须来自同一个 pattern。
    """
    from xiaowei_agent.contracts.base import TRACE_ID_PATTERN
    from xiaowei_agent.trace import _TRACE_ID_RE

    assert TRACE_ID_PATTERN in _TRACE_ID_RE.pattern


def test_generated_trace_id_is_accepted_by_the_contract() -> None:
    """反向断言：生成器产出的值必须能通过契约校验。"""
    from xiaowei_agent.contracts import RequestContext
    from xiaowei_agent.trace import new_trace_id

    ctx = RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id=new_trace_id(),
        policy_revision="policy-2026-09-01",
    )
    assert len(ctx.trace_id) == 32

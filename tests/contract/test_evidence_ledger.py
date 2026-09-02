"""EvidenceLedger：任务作用域的 append-only 证据台账。

存在的理由是 ``TaskOutcome`` 只有 ``evidence_refs``（引用而非内容）——引用隐含一个
可寻址的存储。没有它，Runtime 只能去读 Runner 的内部变量，那既不可审计，也无法在
M4 的跨进程恢复中成立。

**append-only**：证据是已发生事实的记录，覆盖或删除等于篡改审计。
"""

import datetime as dt

import pytest

from xiaowei_agent.contracts import EvidenceEnvelope, ExternalSource
from xiaowei_agent.persistence.evidence import (
    EvidenceConflictError,
    EvidenceLedger,
    EvidenceNotFoundError,
    InMemoryEvidenceLedger,
)

_AT = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
_TASK = "task-1"


def _envelope(evidence_id: str, **overrides: object) -> EvidenceEnvelope:
    base: dict[str, object] = {
        "evidence_id": evidence_id,
        "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0",
        "facts": ({"queryId": "q1"},),
        "source": "starrocks-fake",
        "source_kind": ExternalSource.TOOL,
        "captured_at": _AT,
        "sampled": False,
        "limitations": ("window 30m",),
    }
    return EvidenceEnvelope(**(base | overrides))


@pytest.fixture
def ledger() -> InMemoryEvidenceLedger:
    return InMemoryEvidenceLedger()


def test_ledger_protocol_surface_is_exactly_three_methods() -> None:
    assert {m for m in dir(EvidenceLedger) if not m.startswith("_")} == {
        "append",
        "load",
        "get",
    }


async def test_append_returns_the_addressable_id(ledger: InMemoryEvidenceLedger) -> None:
    returned = await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    assert returned == "task-1:s1"


async def test_load_returns_entries_in_write_order(ledger: InMemoryEvidenceLedger) -> None:
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s2"))
    assert [e.evidence_id for e in await ledger.load(task_id=_TASK)] == [
        "task-1:s1",
        "task-1:s2",
    ]


async def test_ledger_returns_empty_tuple_for_a_task_without_evidence(
    ledger: InMemoryEvidenceLedger,
) -> None:
    """无证据是一个正常状态，不是错误：一次尚未取数的任务就是这样。"""
    assert await ledger.load(task_id="never-written") == ()


async def test_get_resolves_a_written_reference(ledger: InMemoryEvidenceLedger) -> None:
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    assert (await ledger.get(task_id=_TASK, evidence_id="task-1:s1")).evidence_id == (
        "task-1:s1"
    )


async def test_dangling_evidence_reference_raises(ledger: InMemoryEvidenceLedger) -> None:
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    with pytest.raises(EvidenceNotFoundError):
        await ledger.get(task_id=_TASK, evidence_id="task-1:s99")


async def test_ledger_is_append_only(ledger: InMemoryEvidenceLedger) -> None:
    """同一 id 写入不同内容必须拒绝——覆盖等于篡改审计。"""
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    with pytest.raises(EvidenceConflictError):
        await ledger.append(
            task_id=_TASK,
            envelope=_envelope("task-1:s1", facts=({"queryId": "tampered"},)),
        )
    assert len(await ledger.load(task_id=_TASK)) == 1


async def test_rewriting_identical_content_is_idempotent(
    ledger: InMemoryEvidenceLedger,
) -> None:
    """幂等重试不应被当成篡改：内容相同即无事发生。"""
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    await ledger.append(task_id=_TASK, envelope=_envelope("task-1:s1"))
    assert len(await ledger.load(task_id=_TASK)) == 1


async def test_tasks_are_isolated_from_each_other(ledger: InMemoryEvidenceLedger) -> None:
    await ledger.append(task_id="a", envelope=_envelope("a:s1"))
    assert await ledger.load(task_id="b") == ()
    with pytest.raises(EvidenceNotFoundError):
        await ledger.get(task_id="b", evidence_id="a:s1")


async def test_errors_do_not_echo_the_task_id(ledger: InMemoryEvidenceLedger) -> None:
    canary = "task-canary-3319"
    with pytest.raises(EvidenceNotFoundError) as err:
        await ledger.get(task_id=canary, evidence_id="x")
    assert canary not in str(err.value)
    assert err.value.task_id == canary

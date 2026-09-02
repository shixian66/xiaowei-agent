"""EvidenceLedger：任务作用域的 append-only 证据台账。

存在的理由是 ``TaskOutcome`` 只有 ``evidence_refs``（引用而非内容）——引用隐含一个
可寻址的存储。没有它，Runtime 只能去读 Runner 的内部变量，那既不可审计，也无法在
M4 的跨进程恢复中成立。

**append-only**：证据是已发生事实的记录，覆盖或删除等于篡改审计。内容相同的重写
是幂等的（重试不该被当成篡改），内容不同的重写一律拒绝。
"""

import asyncio
from typing import Protocol

from xiaowei_agent.contracts import EvidenceEnvelope


class EvidenceLedgerError(Exception):
    """携带 ``task_id`` 但**不把它放进 ``str(exc)``** 的错误基类。"""

    def __init__(self, message: str, *, task_id: str) -> None:
        super().__init__(message)
        self.task_id = task_id


class EvidenceNotFoundError(EvidenceLedgerError, LookupError):
    """引用悬空：``evidence_refs`` 里的某个 id 在台账里不存在。"""


class EvidenceConflictError(EvidenceLedgerError):
    """同一 evidence_id 已存在且内容不同。"""


class EvidenceLedger(Protocol):
    async def append(self, *, task_id: str, envelope: EvidenceEnvelope) -> str:
        """写入并返回其 ``evidence_id``。

        :raises EvidenceConflictError: 同一 evidence_id 已存在且内容不同。
        """

    async def load(self, *, task_id: str) -> tuple[EvidenceEnvelope, ...]:
        """按写入顺序返回；任务无证据时返回空元组，**不抛异常**。

        无证据是一个正常状态（一次尚未取数的任务就是这样），把它做成异常会迫使
        每个调用方写一段 try/except，而那段代码很容易顺手把"真的没有"和"读失败"
        合并处理。
        """

    async def get(self, *, task_id: str, evidence_id: str) -> EvidenceEnvelope:
        """:raises EvidenceNotFoundError: 引用悬空。"""


class InMemoryEvidenceLedger:
    """单进程实现。

    **只有单进程保证**：跨进程原子性与崩溃恢复要到 M4 才可证。
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, EvidenceEnvelope]] = {}
        self._lock = asyncio.Lock()

    async def append(self, *, task_id: str, envelope: EvidenceEnvelope) -> str:
        async with self._lock:
            task_entries = self._entries.setdefault(task_id, {})
            existing = task_entries.get(envelope.evidence_id)
            if existing is not None and existing != envelope:
                raise EvidenceConflictError(
                    "different evidence already recorded under this id", task_id=task_id
                )
            task_entries[envelope.evidence_id] = envelope
        return envelope.evidence_id

    async def load(self, *, task_id: str) -> tuple[EvidenceEnvelope, ...]:
        async with self._lock:
            # dict 保序，因此这就是写入顺序。
            return tuple(self._entries.get(task_id, {}).values())

    async def get(self, *, task_id: str, evidence_id: str) -> EvidenceEnvelope:
        async with self._lock:
            envelope = self._entries.get(task_id, {}).get(evidence_id)
        if envelope is None:
            raise EvidenceNotFoundError(
                "no evidence recorded under this reference", task_id=task_id
            )
        return envelope

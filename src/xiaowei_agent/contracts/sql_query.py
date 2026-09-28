"""F1 受治理只读查询的契约（ADR-018、设计 §5.6/§9.1）。

SQL 原文只以 bytes 形式存在于两处：``sql_artifacts`` 表与进程内 :class:`HydratedQuery`。
``HydratedQuery`` 刻意不是 Pydantic 契约：它不能被 dump 进 Plan、TaskStore、trace 或
audit，``repr`` 不含 bytes，也不能被 pickle 带出进程。
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final, NoReturn

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, StrictInt, StrictStr
from xiaowei_agent.contracts.enums import Completeness, QueryRequirement, ResultGrantKind

__all__ = [
    "ColumnSpec",
    "Completeness",
    "HydratedQuery",
    "QueryRequirement",
    "ReadonlyQueryBudget",
    "ResultGrantKind",
]

_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


class ReadonlyQueryBudget(Contract):
    """一次只读查询的有界预算；上限即系统硬上限（设计 §8.1）。"""

    preview_max_rows: StrictInt = Field(ge=1, le=1000)
    preview_max_bytes: StrictInt = Field(ge=1_048_576, le=20_971_520)
    query_timeout_seconds: StrictInt = Field(ge=1, le=180)


class ColumnSpec(Contract):
    """按 ordinal 保存的结果列；同名列不合并。"""

    ordinal: StrictInt = Field(ge=0)
    name: StrictStr
    type: StrictStr


@dataclass(frozen=True, slots=True)
class HydratedQuery:
    """进程内的受信 SQL：bytes 的 SHA-256 必须等于绑定的 ``sql_hash``。

    只作为 StepAdmission 与 Gateway 的 keyword-only 参数流转；构造即校验，失败抛
    ``TypeError``/``ValueError``，异常文本不含 SQL。
    """

    sql_ref: str
    sql_hash: str
    sql_bytes: bytes = field(repr=False)
    resource_id: str
    target_fingerprint: str
    config_revision: str
    budget: ReadonlyQueryBudget

    def __post_init__(self) -> None:
        if not isinstance(self.sql_bytes, bytes):
            raise TypeError("sql_bytes must be bytes")
        if not isinstance(self.budget, ReadonlyQueryBudget):
            raise TypeError("budget must be a ReadonlyQueryBudget")
        texts = (self.sql_ref, self.resource_id, self.config_revision)
        if not all(isinstance(value, str) and value for value in texts):
            raise ValueError("HydratedQuery references must be non-empty strings")
        digests = (self.sql_hash, self.target_fingerprint)
        if not all(
            isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None
            for value in digests
        ):
            raise ValueError("HydratedQuery digests must be lowercase SHA-256 hex")
        if hashlib.sha256(self.sql_bytes).hexdigest() != self.sql_hash:
            raise ValueError("sql_bytes do not match sql_hash")

    def __reduce__(self) -> NoReturn:
        raise TypeError("HydratedQuery must not leave the process")

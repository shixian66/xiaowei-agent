"""F1 受治理只读查询的契约（ADR-018、设计 §5.6/§9.1）。

SQL 原文只以 bytes 形式存在于两处：``sql_artifacts`` 表与进程内 :class:`HydratedQuery`。
``HydratedQuery`` 刻意不是 Pydantic 契约：它不能被 dump 进 Plan、TaskStore、trace 或
audit，``repr`` 不含 bytes，也不能被 pickle 带出进程。
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final, NoReturn

from pydantic import Field, field_validator

from xiaowei_agent.contracts.base import Contract, Sha256Hex, StrictInt, StrictStr
from xiaowei_agent.contracts.enums import Completeness, QueryRequirement, ResultGrantKind

__all__ = [
    "CONFIRMED_ARTIFACT_ARGUMENT_KEYS",
    "ColumnSpec",
    "Completeness",
    "HydratedQuery",
    "QualifiedRelation",
    "QueryRequirement",
    "ReadonlyQueryBudget",
    "ResultGrantKind",
    "SqlArtifactRef",
    "confirmed_artifact_arguments",
]

_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


class SqlArtifactRef(Contract):
    """受信 SQL 来源的引用：只来自 ``ArtifactSubmission`` 或目标选择澄清记录，不含原文。"""

    sql_ref: StrictStr = Field(min_length=1)
    sql_hash: Sha256Hex


class ReadonlyQueryBudget(Contract):
    """一次只读查询的有界预算；上限即系统硬上限（设计 §8.1）。"""

    preview_max_rows: StrictInt = Field(ge=1, le=1000)
    preview_max_bytes: StrictInt = Field(ge=1_048_576, le=20_971_520)
    query_timeout_seconds: StrictInt = Field(ge=1, le=180)


class QualifiedRelation(Contract):
    """内部 Catalog 中的 ``database.object``；两段都按小写规范化后比较。

    黑名单只缩小权限面：大小写不同的名字一律视为同一对象，最坏情况是多拒，不会漏拒。
    """

    database: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)

    @field_validator("database", "name", mode="after")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()


class ColumnSpec(Contract):
    """按 ordinal 保存的结果列；同名列不合并。"""

    ordinal: StrictInt = Field(ge=0)
    name: StrictStr
    type: StrictStr


CONFIRMED_ARTIFACT_ARGUMENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "sql_ref",
        "sql_hash",
        "resource_id",
        "target_fingerprint",
        "config_revision",
        "preview_max_rows",
        "preview_max_bytes",
        "query_timeout_seconds",
    }
)
"""``confirmed_artifact`` 步骤 typed_arguments 与 ToolCall.typed_args 的完整键集。

见设计 §5.5/§5.6：只有引用、hash、目标与预算，没有 SQL。
"""


def confirmed_artifact_arguments(
    *,
    sql_ref: str,
    sql_hash: str,
    resource_id: str,
    target_fingerprint: str,
    config_revision: str,
    budget: ReadonlyQueryBudget,
) -> dict[str, str | int]:
    """``confirmed_artifact`` 步骤的全部标量参数：只有引用、hash、目标与预算，没有 SQL。

    PlanCompiler 用它写 typed_arguments，StepAdmission 用它从 HydratedQuery 重算并逐项比对。
    """
    return {
        "sql_ref": sql_ref,
        "sql_hash": sql_hash,
        "resource_id": resource_id,
        "target_fingerprint": target_fingerprint,
        "config_revision": config_revision,
        "preview_max_rows": budget.preview_max_rows,
        "preview_max_bytes": budget.preview_max_bytes,
        "query_timeout_seconds": budget.query_timeout_seconds,
    }


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

    def tool_arguments(self) -> dict[str, str | int]:
        """本查询应有的步骤/ToolCall 标量参数。"""
        return confirmed_artifact_arguments(
            sql_ref=self.sql_ref,
            sql_hash=self.sql_hash,
            resource_id=self.resource_id,
            target_fingerprint=self.target_fingerprint,
            config_revision=self.config_revision,
            budget=self.budget,
        )

    def __reduce__(self) -> NoReturn:
        raise TypeError("HydratedQuery must not leave the process")

"""加载回执与测试结果的存储契约与 PostgreSQL 实现。

两张表分开写而不是合成一张：加载回执按 ``(service_name, provider)`` 由**各进程**
在启动时写，测试结果按 ``check_name`` 由**管理员点击**时写。写入者、主键和时机都
不同，合表会逼出一个既非回执也非结果的中间行。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self

import sqlalchemy as sa
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei_agent.contracts import (
    Contract,
    LoadReceipt,
    LoadStatus,
    StrictInt,
    StrictStr,
    TestResult,
    TestStatus,
)
from xiaowei_agent.persistence.schema import PROVIDER_TEST_STATE, SERVICE_CONFIG_STATE
from xiaowei_agent.persistence.store import Clock

MAX_TEST_DURATION_MS = 600_000


class ProviderStateStoreError(RuntimeError):
    """不把凭据、Provider 原始正文或响应写入异常文本的存储错误。"""


class CheckName(StrEnum):
    """页面上的三个测试项；与 ``provider_test_state`` 的 CHECK 闭集一一对应。

    飞书占两项：凭据与 OAuth callback 是两条独立链路，折成一项就无法表达
    "凭据可用但 callback 不通"。
    """

    GEMINI_CONNECTION = "gemini_connection"
    FEISHU_CREDENTIALS = "feishu_credentials"
    FEISHU_OAUTH = "feishu_oauth"


class ProviderTestErrorCode(StrEnum):
    """测试失败原因的闭集。

    只保存闭集码与一句本地安全提示：Provider 的原始错误正文、原始响应、Token 与
    Secret 都不进这张表——它会被管理面原样读出来显示。
    """

    REAL_TEST_DISABLED = "real_test_disabled"
    NOT_CONFIGURED = "not_configured"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class RecordTestCommand(Contract):
    """一次测试结果的写入。

    ``passed`` 与 ``error_code`` 互斥由 ``model_validator`` 与表上的 CHECK **各守
    一遍**：契约挡住本进程写错，CHECK 挡住任何绕过契约的写入路径。
    """

    check_name: CheckName
    generation: StrictInt = Field(gt=0)
    status: TestStatus
    duration_ms: StrictInt = Field(ge=0, le=MAX_TEST_DURATION_MS)
    error_code: ProviderTestErrorCode | None = None
    error_message: StrictStr | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _failure_facts_are_consistent(self) -> Self:
        if (self.status == "passed") != (self.error_code is None):
            raise ValueError("test failure must carry exactly one closed error code")
        if self.error_message is not None and self.error_code is None:
            raise ValueError("test message requires a failure code")
        return self


@dataclass(frozen=True, slots=True)
class ProviderStateSnapshot:
    """页面计算状态所需的全部持久化事实。

    **刻意不用 ``Contract``**：两个映射的键分别是 ``(service_name, provider)``
    元组与 ``check_name``，而 ``FrozenMap`` 只支持字符串键到标量。这里沿用它同一条
    纪律——先 ``dict()`` 复制切断与调用方原对象的联系，再 ``MappingProxyType``
    包装挡住原地改写；两步缺一不可。
    """

    receipts: Mapping[tuple[str, str], LoadReceipt]
    tests: Mapping[str, TestResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipts", MappingProxyType(dict(self.receipts)))
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))


class ProviderStateStore(Protocol):
    """加载回执与测试结果的读写边界。"""

    async def record_load(
        self, *, receipts: Mapping[tuple[str, str], LoadReceipt]
    ) -> None:
        """整批覆盖本进程这一轮的加载回执；空映射是合法的空操作。"""

    async def record_test(self, *, command: RecordTestCommand) -> None:
        """按 ``check_name`` 覆盖式写入最新一次测试结果。"""

    async def snapshot(self) -> ProviderStateSnapshot:
        """读出全部回执与全部测试结果。"""


class PostgresProviderStateStore:
    """``ProviderStateStore`` 的 PostgreSQL 实现。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def record_load(
        self, *, receipts: Mapping[tuple[str, str], LoadReceipt]
    ) -> None:
        if not receipts:
            # 没有回执是**正常状态**（文件缺失或整体损坏时读不出可信 generation），
            # 不是"没什么可写"就顺手把旧行删掉——旧回执仍是"上次加载了第几代"的事实。
            return
        now = self._clock()
        rows = [
            {
                "service_name": service_name,
                "provider": provider,
                "loaded_generation": receipt.generation,
                "load_status": receipt.status,
                "loaded_at": now,
            }
            for (service_name, provider), receipt in sorted(receipts.items())
        ]
        statement = sa.dialects.postgresql.insert(SERVICE_CONFIG_STATE)
        async with self._engine.begin() as connection:
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        SERVICE_CONFIG_STATE.c.service_name,
                        SERVICE_CONFIG_STATE.c.provider,
                    ],
                    set_={
                        "loaded_generation": statement.excluded.loaded_generation,
                        "load_status": statement.excluded.load_status,
                        "loaded_at": statement.excluded.loaded_at,
                    },
                ),
                rows,
            )

    async def record_test(self, *, command: RecordTestCommand) -> None:
        now = self._clock()
        statement = sa.dialects.postgresql.insert(PROVIDER_TEST_STATE).values(
            check_name=command.check_name.value,
            tested_generation=command.generation,
            test_status=command.status,
            tested_at=now,
            duration_ms=command.duration_ms,
            error_code=None if command.error_code is None else command.error_code.value,
            error_message=command.error_message,
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[PROVIDER_TEST_STATE.c.check_name],
                    set_={
                        "tested_generation": statement.excluded.tested_generation,
                        "test_status": statement.excluded.test_status,
                        "tested_at": statement.excluded.tested_at,
                        "duration_ms": statement.excluded.duration_ms,
                        "error_code": statement.excluded.error_code,
                        "error_message": statement.excluded.error_message,
                    },
                )
            )

    async def snapshot(self) -> ProviderStateSnapshot:
        async with self._engine.connect() as connection:
            receipt_rows = (
                (await connection.execute(sa.select(SERVICE_CONFIG_STATE)))
                .mappings()
                .all()
            )
            test_rows = (
                (await connection.execute(sa.select(PROVIDER_TEST_STATE)))
                .mappings()
                .all()
            )
        return ProviderStateSnapshot(
            receipts={
                (row["service_name"], row["provider"]): LoadReceipt(
                    generation=row["loaded_generation"],
                    status=row["load_status"],
                )
                for row in receipt_rows
            },
            tests={
                row["check_name"]: TestResult(
                    status=row["test_status"], generation=row["tested_generation"]
                )
                for row in test_rows
            },
        )


__all__ = [
    "MAX_TEST_DURATION_MS",
    "CheckName",
    "LoadStatus",
    "PostgresProviderStateStore",
    "ProviderStateSnapshot",
    "ProviderStateStore",
    "ProviderStateStoreError",
    "ProviderTestErrorCode",
    "RecordTestCommand",
]

"""慢查询参数 schema、时间窗规范化与 ``typed_arguments`` 往返。

三条设计要点：

1. **标识符槽位是闭集形状。** ``database`` / ``user_name`` / ``query_id`` 来自
   ``IntentDraft`` 的槽位，即模型输出。把它们收进一条正则，等于在**进入编译器
   之前**就消灭引号、分号、空格、注释符、全角字符与隐藏字符。
2. **时间窗必须向下取整到整分钟。** 不取整时，同一个 ``IntentDraft`` 在相邻两秒
   会编译出不同的 ``typed_arguments``，``plan_hash`` 随之变化，"固定输入产生同一
   计划"这条退出标准在物理上无法满足，幂等重试也会变成两个不同的计划。
3. **参数不携带执行上下文。** ``environment_id`` / ``tenant_id`` / ``actor`` 不是
   SQL 参数：它们由 ``target_fingerprint`` 绑定（ARCHITECTURE §7.2）。机械复制会
   让同一事实在两个 DTO 里各有一份真源，两者漂移时没有哪一份更权威。
"""

import datetime as _dt
import re
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Final, Self

from pydantic import AfterValidator, Field, model_validator

from xiaowei_agent.contracts import (
    MAX_ROW_LIMIT,
    MAX_WINDOW_MINUTES,
    AwareDatetime,
    CapabilityParams,
    JsonScalar,
    StrictInt,
    StrictStr,
)

DEFAULT_WINDOW_MINUTES: Final[int] = 30
DEFAULT_ROW_LIMIT: Final[int] = 20
DEFAULT_MIN_QUERY_TIME_MS: Final[int] = 10_000
"""慢查询阈值默认值，来自旧项目 ivor_aiops 的 ``query_time_high_ms`` 实证值。"""

MAX_MIN_QUERY_TIME_MS: Final[int] = 3_600_000
"""阈值上限：1 小时。超过窗口跨度的阈值没有语义。"""

SQL_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
"""SQL 时间字面量格式的**唯一定义处**。

compiler 用它生成字面量，SQLGuard 用它把 AST 里的字面量解析回来比对。两处各写
一份是"声明口径与校验口径悄悄漂移"这类缺陷的通用形状：happy path 先坏，或者
更糟——校验形同虚设而没人发现。
"""

ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"sql", "sql_template_id"})
"""步骤 ``typed_arguments`` 里**非参数**的两个键。

它们是编译器写进步骤的信封字段（SQL 原文与模板 id），不是 SQL 参数。
``from_typed_arguments`` 显式跳过这两个**已声明**的键——这与"忽略未知键"是两回事：
其余任何多出来的键仍然被拒绝。
"""

_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_$-]{0,63}\Z")


def _identifier(value: str) -> str:
    """标识符槽位的闭集形状。

    :raises ValueError: 取值不是朴素 SQL 标识符。错误文本不含取值本身。
    """
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError("must be a plain SQL identifier")
    return value


Identifier = Annotated[StrictStr, AfterValidator(_identifier)]


class SlowQueryParams(CapabilityParams):
    """一次慢查询取数的全部 SQL 参数。

    可选过滤为 ``None`` 表示"不按该维度过滤"，不是"过滤成空"。
    """

    INPUT_SCHEMA_REF: ClassVar[str] = "input.starrocks.slow_query.v1"

    window_start: AwareDatetime
    window_end: AwareDatetime
    min_query_time_ms: StrictInt = Field(
        default=DEFAULT_MIN_QUERY_TIME_MS, ge=0, le=MAX_MIN_QUERY_TIME_MS
    )
    row_limit: StrictInt = Field(ge=1, le=MAX_ROW_LIMIT)
    database: Identifier | None = None
    user_name: Identifier | None = None
    query_id: Identifier | None = None

    @model_validator(mode="after")
    def _window_is_bounded_and_ordered(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be strictly after window_start")
        if self.window_end - self.window_start > _dt.timedelta(minutes=MAX_WINDOW_MINUTES):
            raise ValueError("window exceeds the maximum span")
        return self

    def to_typed_arguments(self) -> dict[str, JsonScalar]:
        """投影成计划步骤可携带的标量映射。

        时间一律转 ISO 字符串：``typed_arguments`` 进 ``plan_hash``，而 hash 的
        规范输入只接受 JSON 标量。
        """
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "min_query_time_ms": self.min_query_time_ms,
            "row_limit": self.row_limit,
            "database": self.database,
            "user_name": self.user_name,
            "query_id": self.query_id,
        }

    @classmethod
    def from_typed_arguments(cls, arguments: Mapping[str, JsonScalar]) -> Self:
        """从计划步骤的 ``typed_arguments`` 还原参数，**未知键与缺失键一律拒绝**。

        准入路径拿到的是 ``typed_arguments``（标量映射），而 SQLGuard 需要的是
        已校验的参数对象。缺了这个还原入口，准入就得自己拆字典——一旦拆错或漏
        校验，规则 0 的重编译比对就会基于一份未经校验的参数进行，比对本身失去意义。

        还原走的是与首次构造**同一条**校验（``model_validate``），因此不存在
        "从存储读回就不校验"的旁路。

        :raises ValidationError: 存在信封外的未知键、缺少必填键，或任一取值非法。
        """
        payload: dict[str, Any] = {
            key: value for key, value in arguments.items() if key not in ENVELOPE_KEYS
        }
        for field in ("window_start", "window_end"):
            raw = payload.get(field)
            if isinstance(raw, str):
                payload[field] = _parse_timestamp(raw)
        return cls.model_validate(payload)


def _parse_timestamp(value: str) -> _dt.datetime | str:
    """把 ISO 字符串解析回 aware datetime。

    解析失败时**原样返回**，交由字段校验产生结构化 ``ValidationError``；在这里
    自行抛 ``ValueError`` 会让还原入口有两种不同形状的失败。
    """
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return value


def normalise_window(
    *, as_of: _dt.datetime, window_minutes: int
) -> tuple[_dt.datetime, _dt.datetime]:
    """把 ``as_of`` 向下取整到整分钟 UTC，回传 ``[start, end)``。

    :raises ValueError: ``as_of`` 不带时区，或窗口跨度不在 ``(0, 上限]`` 内。
    """
    if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("as_of must be timezone-aware")
    if not 0 < window_minutes <= MAX_WINDOW_MINUTES:
        raise ValueError("window span is outside the allowed range")
    end = as_of.astimezone(_dt.UTC).replace(second=0, microsecond=0)
    return end - _dt.timedelta(minutes=window_minutes), end

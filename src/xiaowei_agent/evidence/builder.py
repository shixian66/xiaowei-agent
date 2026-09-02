"""把一次工具调用的结果构造成 ``EvidenceEnvelope``。

**纯函数**：无 async、无 I/O、只依赖 ``contracts``。写入 ledger 是调用方（Runner）
的事——本模块只负责"把已发生的事实包起来"，不决定任何事情。

三条构造规则：

1. **capability 身份取自计划，不取自 adapter。** adapter 的返回值是外部内容；让它
   决定证据属于哪个能力，等于让外部输入改写审计。
2. **facts 按列白名单过滤。** Gateway 的归一化管的是**形状**，不是**字段集**；
   adapter 多返回的列必须在这里丢弃，否则 ``stmt`` 一类携带 SQL 原文的列会一路
   进入证据与渲染。
3. **limitations 是确定性文案。** 它说明窗口、目标范围、行数上限与阈值，**不含
   任何外部文本**——外部错误只以 ``ExternalContent`` 摘要引用的形式存在于
   ``ToolResult.error``。
"""

import datetime as _dt
from typing import Final, Protocol

from xiaowei_agent.contracts import (
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalSource,
    FrozenMap,
    JsonScalar,
    PlanStep,
    SqlSurface,
    ToolResult,
)

_EVIDENCE_ID_SEPARATOR: Final[str] = ":"


class SlowQueryScope(Protocol):
    """本模块需要的参数**形状**，用结构化类型表达。

    写成 Protocol 而不是 import ``SlowQueryParams``：``evidence`` 只允许依赖
    ``contracts``（纯度测试承重），而参数 DTO 属 ``planning``。结构化类型让本模块
    既拿到静态类型检查，又不引入那条会让纯度失效的依赖边。
    """

    @property
    def window_start(self) -> _dt.datetime: ...

    @property
    def window_end(self) -> _dt.datetime: ...

    @property
    def row_limit(self) -> int: ...

    @property
    def min_query_time_ms(self) -> int: ...

    @property
    def database(self) -> str | None: ...

    @property
    def user_name(self) -> str | None: ...

    @property
    def query_id(self) -> str | None: ...


def evidence_id(*, task_id: str, step_id: str) -> str:
    """证据的可寻址标识。

    确定性、可复现，同时是 ``EvidenceLedger`` 的键与 ``TaskOutcome.evidence_refs``
    的元素——三处用同一个函数，引用才不会各自拼一份而悄悄漂移。
    """
    return f"{task_id}{_EVIDENCE_ID_SEPARATOR}{step_id}"


def _filtered(row: FrozenMap, surface: SqlSurface) -> dict[str, JsonScalar]:
    allowed = set(surface.allowed_columns) | set(surface.allowed_output_aliases)
    return {key: value for key, value in row.items() if key in allowed}


_LIMITATION_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def _limitations(
    *, params: SlowQueryScope, surface: SqlSurface, row_count: int, sampled: bool
) -> tuple[str, ...]:
    """确定性限制文案。**不含任何外部文本。**"""
    start = params.window_start.strftime(_LIMITATION_TIME_FORMAT)
    end = params.window_end.strftime(_LIMITATION_TIME_FORMAT)
    scope = [
        f"window: [{start}, {end})",
        f"row limit: {params.row_limit}",
        f"slow query threshold ms: {params.min_query_time_ms}",
        f"returned rows: {row_count}",
    ]
    for label, value in (
        ("database", params.database),
        ("user", params.user_name),
        ("query id", params.query_id),
    ):
        scope.append(f"{label} filter: {value if value is not None else 'none'}")
    if sampled:
        scope.append("row limit reached; results may be truncated")
    scope.append(f"columns limited to the declared surface: {surface.surface_id}")
    return tuple(scope)


def build_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    result: ToolResult,
    surface: SqlSurface,
    params: SlowQueryScope,
    captured_at: _dt.datetime,
) -> EvidenceEnvelope:
    """构造一份证据信封。

    :param params: 本步骤的已校验 SQL 参数；只用于生成确定性限制文案。
    :param captured_at: 注入的时钟读数——证据的时间不能来自模块内部的 ``now()``，
        否则同一次运行不可复现。
    """
    facts = tuple(_filtered(row, surface) for row in result.data_view)
    sampled = params.row_limit > 0 and len(facts) >= params.row_limit
    return EvidenceEnvelope(
        evidence_id=evidence_id(task_id=task_id, step_id=step.step_id),
        # 取自计划，不取自 adapter。
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        facts=facts,
        source=result.source,
        source_kind=ExternalSource.TOOL,
        captured_at=captured_at,
        sampled=sampled,
        limitations=_limitations(
            params=params, surface=surface, row_count=len(facts), sampled=sampled
        ),
        # facts 已按列白名单裁剪，没有需要引用的脱敏记录。
        redaction_ref=None,
    )

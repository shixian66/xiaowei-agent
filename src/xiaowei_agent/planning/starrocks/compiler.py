"""确定性 SQL 编译器。

**SQL 由 sqlglot 表达式树构造后序列化，禁止字符串拼接。** 字面量一律
``exp.Literal``，标识符一律 ``exp.to_identifier(..., quoted=True)``。
``test_compiler_never_interpolates_raw_strings`` 用 AST 扫描承重这条禁令。

核心是**唯一的目标范围谓词构造器** :func:`_scope_predicates`：两条模板共用它，
是 count 模板语义正确的唯一保证。count 要回答的是「目标范围内有没有任何查询」；
若它只按同表同窗口计数，窗口里存在其他库/用户的流量时，「目标范围无审计数据」
就会被误报成「目标范围无慢查询」——一个自信但错误的结论。共用构造器让两条模板
在结构上不可能各自演化。
"""

import datetime as _dt
import functools
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from sqlglot import exp

from xiaowei_agent.capabilities.effect import build_plan_step
from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST
from xiaowei_agent.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionPlan,
    JsonScalar,
    PlanBudget,
    PlanStep,
    RequestContext,
    ResolvedTarget,
    SqlSurface,
    StepCondition,
    StepConditionKind,
)
from xiaowei_agent.planning.starrocks.params import SQL_TIME_FORMAT, SlowQueryParams

LIST_V1: Final[str] = "starrocks.slow_query.list.v1"
COUNT_V1: Final[str] = "starrocks.slow_query.count.v1"

TEMPLATE_IDS: Final[tuple[str, ...]] = (LIST_V1, COUNT_V1)

ALLOWED_DIALECTS: Final[frozenset[str]] = frozenset({"starrocks"})
"""编译器与 SQLGuard 共用的方言闭集。

编译器也校验方言，而不是只让 Guard 校验：否则一份被篡改 ``dialect`` 的 surface
能让我们**先生成**另一种语法的 SQL，再由 Guard 以别的理由拒绝——拒绝原因会指向
错误的阶段，错误归因随之失真。
"""

COUNT_ALIAS: Final[str] = "query_count"
COUNT_ROW_LIMIT: Final[int] = 1
"""count 模板恒为 ``LIMIT 1``：它只需要回答"有没有"，不需要取回行。"""

_LIST_COLUMNS: Final[tuple[str, ...]] = (
    "queryId",
    "timestamp",
    "queryTime",
    "scanRows",
    "returnRows",
    "scanBytes",
    "memCostBytes",
    "pendingTimeMs",
    "cpuCostNs",
    "state",
    "errorCode",
    "db",
    "user",
)

_SCOPE_PARAM_KEYS: Final[tuple[str, ...]] = (
    "window_start",
    "window_end",
    "database",
    "user_name",
    "query_id",
)

_SCOPE_COLUMN: Final[Mapping[str, str]] = MappingProxyType(
    {"database": "db", "user_name": "user", "query_id": "queryId"}
)
"""可选过滤的槽位名 → 列名。

**顺序即编译顺序**（db → user → queryId）：顺序不定会让同一组参数产生两个
``plan_hash``。Python 3.7+ 的 dict 保序，这里依赖的正是这一点。
"""

TEMPLATE_PARAM_KEYS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        LIST_V1: (*_SCOPE_PARAM_KEYS, "min_query_time_ms", "row_limit"),
        COUNT_V1: (*_SCOPE_PARAM_KEYS, "row_limit"),
    }
)
"""两条模板各自**消费**的参数键集。

与 SQL 层的集合差断言配对：同一条不变量在参数层与 SQL 层都钉住，才不会一处改了
另一处没改。
"""

_ORDER_BY: Final[tuple[tuple[str, bool], ...]] = (
    ("queryTime", True),
    ("timestamp", True),
    ("queryId", False),
)
"""三键全序。

只按 ``queryTime`` 排序时，同耗时行的顺序由存储引擎决定，``data_view`` 不可
复现，golden eval 会随机失败。
"""


def _column(name: str) -> exp.Column:
    return exp.column(name, quoted=True)


def _timestamp_literal(value: _dt.datetime) -> exp.Literal:
    """把窗口边界渲染成 SQL 字面量。

    格式取自 ``SQL_TIME_FORMAT`` 这一唯一定义处，SQLGuard 用同一个常量把字面量
    解析回来比对。
    """
    return exp.Literal.string(value.strftime(SQL_TIME_FORMAT))


def _scope_predicates(params: SlowQueryParams, surface: SqlSurface) -> list[exp.Expression]:
    """**目标范围**谓词：时间窗 + 全部已给定的目标过滤。

    两条模板共用这一个构造器，是 count 模板语义正确的唯一保证（见模块 docstring）。

    可选过滤按固定顺序追加（db → user → queryId）。
    """
    time_column = _column(surface.time_column)
    predicates: list[exp.Expression] = [
        exp.GTE(this=time_column, expression=_timestamp_literal(params.window_start)),
        exp.LT(this=time_column.copy(), expression=_timestamp_literal(params.window_end)),
    ]
    for slot, column in _SCOPE_COLUMN.items():
        value = getattr(params, slot)
        if value is not None:
            predicates.append(
                exp.EQ(this=_column(column), expression=exp.Literal.string(value))
            )
    return predicates


def _where(predicates: Sequence[exp.Expression]) -> exp.Where:
    """把谓词折成一条左深 ``AND`` 链。

    用一次 ``set("where", ...)`` 而不是反复 ``.where()``：后者每次都会把已累积的
    条件包一层，序列化出多余的括号，golden 逐字节断言随之不稳定。
    """
    return exp.Where(this=functools.reduce(lambda a, b: exp.And(this=a, expression=b), predicates))


def _table(surface: SqlSurface) -> exp.Table:
    """唯一允许表。

    从 surface 的声明反解出库名与表名，使"声明什么就发出什么"——编译器里不另写
    一份表名常量。
    """
    database, _, name = surface.allowed_tables[0].partition(".")
    return exp.Table(
        this=exp.to_identifier(name, quoted=True),
        db=exp.to_identifier(database, quoted=True),
    )


def _compile_list(params: SlowQueryParams, surface: SqlSurface) -> exp.Select:
    select = (
        exp.Select()
        .select(*[_column(name) for name in _LIST_COLUMNS])
        .from_(_table(surface))
    )
    predicates = [
        *_scope_predicates(params, surface),
        exp.GTE(
            this=_column("queryTime"),
            expression=exp.Literal.number(params.min_query_time_ms),
        ),
    ]
    select.set("where", _where(predicates))
    for name, descending in _ORDER_BY:
        select = select.order_by(
            # nulls_first 显式给出：与方言默认不一致时 sqlglot 会展开成 CASE
            # WHEN ... IS NULL 表达式，那既让 golden 不稳定，也会引入不在节点
            # 闭集内的 Case/If/Null 节点。
            exp.Ordered(this=_column(name), desc=descending, nulls_first=not descending)
        )
    return select.limit(exp.Literal.number(params.row_limit))


def _compile_count(params: SlowQueryParams, surface: SqlSurface) -> exp.Select:
    select = (
        exp.Select()
        .select(exp.alias_(exp.Count(this=exp.Star()), COUNT_ALIAS, quoted=True))
        .from_(_table(surface))
    )
    select.set("where", _where(_scope_predicates(params, surface)))
    return select.limit(exp.Literal.number(COUNT_ROW_LIMIT))


def compile_sql(*, template_id: str, params: SlowQueryParams, surface: SqlSurface) -> str:
    """把已校验参数编译成一条 SQL。

    同一 ``(template_id, params, surface)`` 必然产生**逐字节相同**的结果；准入时
    的重编译比对正是依赖这一点。

    :raises ValueError: 模板 id 不在闭集内，或 surface 的方言不在闭集内。
    """
    if template_id not in TEMPLATE_PARAM_KEYS:
        raise ValueError("unknown sql template")
    if surface.dialect not in ALLOWED_DIALECTS:
        raise ValueError("unknown sql dialect")
    builder = _compile_list if template_id == LIST_V1 else _compile_count
    return builder(params, surface).sql(dialect=surface.dialect)


# --- 计划编译 -----------------------------------------------------------------

STEP_LIST: Final[str] = "s1"
STEP_COUNT: Final[str] = "s2"

PLAN_BUDGET: Final[PlanBudget] = PlanBudget(
    max_steps=2, max_tool_calls=2, max_model_tokens=4000
)

_COUNT_CONDITION: Final[StepCondition] = StepCondition(
    kind=StepConditionKind.EVIDENCE_ROW_COUNT_BELOW,
    ref_step_id=STEP_LIST,
    threshold=1,
)
"""s2 的执行条件：s1 一行都没取到时才跑。

条件是**闭集枚举成员**，不是谓词表达式：它无法引用模型输出、无法承载代码，
新增一种条件必须改 ``StepConditionKind`` 并过评审（ARCHITECTURE §4.2）。
"""

_PLAN_SHAPE: Final[
    tuple[tuple[str, str, str, tuple[str, ...], StepCondition | None], ...]
] = (
    (STEP_LIST, OP_LIST, LIST_V1, (), None),
    (STEP_COUNT, OP_COUNT, COUNT_V1, (STEP_LIST,), _COUNT_CONDITION),
)
"""计划形状是**编译器的确定性决定**。

步骤、顺序、依赖与条件都写在这里，不由候选、模型或用户挑选——决策权责矩阵
（ARCHITECTURE §4.3）把"步骤与工具调用顺序"划归 Resolver + PlanCompiler。
"""

ENTRY_OPERATION: Final[str] = OP_LIST
"""计划的入口 operation。候选必须指向它，否则拒绝。"""


def _step_arguments(
    *, template_id: str, params: SlowQueryParams, surface: SqlSurface
) -> dict[str, JsonScalar]:
    """步骤携带的 ``typed_arguments``：SQL 信封 + 该模板消费的参数子集。

    只放该模板真正消费的参数键：多出来的键会进 ``plan_hash``，成为"看起来生效
    但其实不影响执行"的第二真源。
    """
    arguments = params.to_typed_arguments()
    return {
        "sql": compile_sql(template_id=template_id, params=params, surface=surface),
        "sql_template_id": template_id,
        **{key: arguments[key] for key in TEMPLATE_PARAM_KEYS[template_id]},
    }


def compile_plan(
    *,
    candidate: Candidate,
    params: SlowQueryParams,
    target: ResolvedTarget,
    context: RequestContext,
    snapshot: CapabilitySnapshot,
    surface: SqlSurface,
) -> ExecutionPlan:
    """把一个已解析候选编译成确定性 ``ExecutionPlan``。

    同一组输入必然产生逐字节相同的计划与 ``plan_hash``；审批绑定与恢复时的漂移
    检测都依赖这一点。

    分类字段一律经 :func:`build_plan_step` 从快照派生——编译器无从设置、覆盖或
    降级 ``side_effect`` 与 ``effect_class``。

    :raises ValueError: 候选不指向入口 operation，目标与上下文的环境不一致，
        或快照里没有该候选声明的能力。
    """
    if candidate.operation != ENTRY_OPERATION:
        raise ValueError("candidate does not point at the plan entry operation")
    if target.environment_id != context.environment_id:
        raise ValueError("target environment does not match the request context")
    spec = next(
        (
            item
            for item in snapshot.specs
            if item.capability_id == candidate.capability_id
            and item.version == candidate.capability_version
        ),
        None,
    )
    if spec is None:
        raise ValueError("candidate capability is absent from the snapshot")

    steps: list[PlanStep] = [
        build_plan_step(
            snapshot,
            capability_id=candidate.capability_id,
            capability_version=candidate.capability_version,
            operation=operation,
            step_id=step_id,
            typed_arguments=_step_arguments(
                template_id=template_id, params=params, surface=surface
            ),
            depends_on=depends_on,
            condition=condition,
        )
        for step_id, operation, template_id, depends_on, condition in _PLAN_SHAPE
    ]
    return ExecutionPlan(
        capability_id=candidate.capability_id,
        capability_version=candidate.capability_version,
        steps=tuple(steps),
        # policy_profile 取自快照声明，不写死在编译器里：profile 变更必须经能力
        # 声明并过评审，编译器不是第二个声明处。
        policy_profile=spec.policy_profile,
        policy_revision=context.policy_revision,
        budget=PLAN_BUDGET,
    )

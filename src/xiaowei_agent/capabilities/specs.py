"""``starrocks.slow_query.diagnose`` 的能力声明与 SQL 面实例。

本模块只做**声明**：它不解析候选、不编译计划、不执行任何东西。
``side_effect`` 与 ``effect_class`` 的唯一确定性来源就是这里的 ``OperationSpec``
（ADR-007 D7）——模型、用户输入与 adapter 都不得设置、覆盖或降级这两个字段。
"""

from typing import Final

from xiaowei_agent.contracts import (
    MAX_ROW_LIMIT,
    MAX_WINDOW_MINUTES,
    CapabilitySpec,
    EffectClass,
    OperationSpec,
    SqlSurface,
)

CAPABILITY_ID: Final[str] = "starrocks.slow_query.diagnose"
CAPABILITY_VERSION: Final[str] = "1.0.0"
OP_LIST: Final[str] = "list_slow_queries"
OP_COUNT: Final[str] = "count_queries_in_window"
POLICY_PROFILE: Final[str] = "readonly.starrocks.slow_query.v1"

GATEWAY_NAME: Final[str] = "starrocks"
"""``ToolCall.gateway`` 的取值：Gateway 按它选择 adapter。"""

SLOW_QUERY_SPEC: Final[CapabilitySpec] = CapabilitySpec(
    capability_id=CAPABILITY_ID,
    version=CAPABILITY_VERSION,
    domain="starrocks",
    operations=(
        OperationSpec(
            operation=OP_LIST,
            gateway=GATEWAY_NAME,
            effect_class=EffectClass.READ,
            side_effect=False,
            argument_schema_ref="schema.starrocks.slow_query.list.v1",
        ),
        OperationSpec(
            operation=OP_COUNT,
            gateway=GATEWAY_NAME,
            effect_class=EffectClass.READ,
            side_effect=False,
            argument_schema_ref="schema.starrocks.slow_query.count.v1",
        ),
    ),
    policy_profile=POLICY_PROFILE,
    evidence_contract="evidence.starrocks.slow_query.v1",
    eval_ref="evals.starrocks.slow_query.v1",
)

SLOW_QUERY_SURFACE: Final[SqlSurface] = SqlSurface(
    surface_id="sql.starrocks.slow_query.v1",
    dialect="starrocks",
    allowed_tables=("starrocks_audit_db__.starrocks_audit_tbl__",),
    # 列名来自旧项目 ivor_aiops 的规范名与实际下发 SQL。**刻意排除** clientIp
    # （只在别名表出现）、digest（旧项目有四种拼法，真实列名不可确定）、stmt
    # （SQL 原文，会携带字面量）。这些列名未在真实 StarRocks 上核对过（M0-M6a
    # 禁止连接），M6b 首次连接时必须用 SHOW CREATE TABLE 核对并回修。
    allowed_columns=(
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
    ),
    allowed_output_aliases=("query_count",),
    time_column="timestamp",
    max_row_limit=MAX_ROW_LIMIT,
    max_window_minutes=MAX_WINDOW_MINUTES,
)

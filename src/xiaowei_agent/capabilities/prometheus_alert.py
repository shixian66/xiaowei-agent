"""``prometheus.alert.evidence`` 的版本化声明与 PromQL 表面。"""

from typing import Final

from xiaowei_agent.contracts import (
    MAX_PROMQL_POINTS_PER_SERIES,
    MAX_PROMQL_SERIES,
    MAX_PROMQL_WINDOW_MINUTES,
    CapabilitySpec,
    EffectClass,
    OperationSpec,
    PromqlSurface,
    ReadClass,
)

PROMETHEUS_ALERT_CAPABILITY_ID: Final[str] = "prometheus.alert.evidence"
PROMETHEUS_ALERT_CAPABILITY_VERSION: Final[str] = "1.0.0"
OP_GET_ACTIVE_ALERTS: Final[str] = "get_active_alerts"
OP_QUERY_METRIC_RANGE: Final[str] = "query_metric_range"
PROMETHEUS_ALERT_POLICY_PROFILE: Final[str] = (
    "readonly.prometheus.alert.evidence.v1"
)
PROMETHEUS_ALERT_INPUT_SCHEMA_REF: Final[str] = "input.prometheus.alert.v1"

ALERTMANAGER_GATEWAY: Final[str] = "alertmanager"
PROMETHEUS_GATEWAY: Final[str] = "prometheus"
CPU_PERCENT_TEMPLATE_V1: Final[str] = "prometheus.alert.host_cpu_percent.v1"
INSTANCE_UP_TEMPLATE_V1: Final[str] = "prometheus.alert.instance_up.v1"

PROMETHEUS_ENVIRONMENT_IDS: Final[tuple[str, ...]] = ("dev", "test")
"""fake 能力允许的非生产环境闭集。"""

PROMETHEUS_ALERT_SPEC: Final[CapabilitySpec] = CapabilitySpec(
    capability_id=PROMETHEUS_ALERT_CAPABILITY_ID,
    version=PROMETHEUS_ALERT_CAPABILITY_VERSION,
    domain="prometheus",
    input_schema_ref=PROMETHEUS_ALERT_INPUT_SCHEMA_REF,
    operations=(
        OperationSpec(
            operation=OP_GET_ACTIVE_ALERTS,
            gateway=ALERTMANAGER_GATEWAY,
            effect_class=EffectClass.READ,
            read_class=ReadClass.BOUNDED,
            side_effect=False,
            argument_schema_ref="schema.alertmanager.active_alerts.v1",
        ),
        OperationSpec(
            operation=OP_QUERY_METRIC_RANGE,
            gateway=PROMETHEUS_GATEWAY,
            effect_class=EffectClass.READ,
            read_class=ReadClass.BOUNDED,
            side_effect=False,
            argument_schema_ref="schema.prometheus.metric_range.v1",
        ),
    ),
    policy_profile=PROMETHEUS_ALERT_POLICY_PROFILE,
    evidence_contract="evidence.prometheus.alert.v1",
    eval_ref="evals.prometheus.alert.v1",
)

PROMQL_SURFACE: Final[PromqlSurface] = PromqlSurface(
    surface_id="promql.prometheus.alert.evidence.v1",
    allowed_template_ids=(
        CPU_PERCENT_TEMPLATE_V1,
        INSTANCE_UP_TEMPLATE_V1,
    ),
    max_window_minutes=MAX_PROMQL_WINDOW_MINUTES,
    max_series=MAX_PROMQL_SERIES,
    max_points_per_series=MAX_PROMQL_POINTS_PER_SERIES,
)

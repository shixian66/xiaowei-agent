"""Prometheus 告警实例的确定性目标解析。"""

from xiaowei_agent.capabilities.prometheus_alert import (
    PROMETHEUS_ENVIRONMENT_IDS,
)
from xiaowei_agent.contracts import RequestContext, ResolvedTarget
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams


def resolve_prometheus_alert_target(
    *, context: RequestContext, params: PrometheusAlertParams
) -> ResolvedTarget:
    """只从受信 context 与已规范化参数构造逻辑目标。"""
    if context.environment_id not in PROMETHEUS_ENVIRONMENT_IDS:
        raise ValueError("environment is outside the Prometheus fake directory")
    return ResolvedTarget(
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
        provider="prometheus",
        resource_kind="alert_instance",
        resource_ids=(
            f"alert:{params.alert_name}",
            f"instance:{params.instance}",
        ),
        selector_version="prometheus.alert.instance.v1",
    )

"""能力声明与 Registry 快照。

Registry 只给出**某一时刻的快照**：同一次请求内看到的能力集合必须确定，否则
候选解析与分类派生会依赖调用时刻。
"""

from xiaowei_agent.capabilities.prometheus_alert import (
    ALERTMANAGER_GATEWAY,
    PROMETHEUS_ALERT_CAPABILITY_ID,
    PROMETHEUS_ALERT_CAPABILITY_VERSION,
    PROMETHEUS_ALERT_SPEC,
    PROMETHEUS_GATEWAY,
)
from xiaowei_agent.capabilities.registry import SNAPSHOT_ID, StaticCapabilityRegistry
from xiaowei_agent.capabilities.specs import (
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    OP_COUNT,
    OP_LIST,
    POLICY_PROFILE,
    SLOW_QUERY_SPEC,
    SLOW_QUERY_SURFACE,
)
from xiaowei_agent.contracts import EffectClass
from xiaowei_agent.contracts.sql_surface import MAX_ROW_LIMIT, MAX_WINDOW_MINUTES


def test_snapshot_is_stable_across_calls() -> None:
    registry = StaticCapabilityRegistry()
    assert registry.snapshot() == registry.snapshot()


def test_snapshot_id_is_stable_across_instances() -> None:
    """快照 id 不得随实例或时刻变化——它会进候选集合与审计。"""
    assert StaticCapabilityRegistry().snapshot().snapshot_id == (
        StaticCapabilityRegistry().snapshot().snapshot_id
    )


def test_snapshot_id_and_ordered_spec_set_are_one_golden() -> None:
    snapshot = StaticCapabilityRegistry().snapshot()
    assert (
        snapshot.snapshot_id,
        tuple((spec.capability_id, spec.version) for spec in snapshot.specs),
    ) == (
        "snapshot.m6a.starrocks-prometheus.v1",
        (
            ("starrocks.slow_query.diagnose", "1.0.0"),
            ("prometheus.alert.evidence", "1.0.0"),
        ),
    )
    assert SNAPSHOT_ID == snapshot.snapshot_id


def test_synthetic_write_capability_is_not_registered() -> None:
    """合成副作用能力只存在于测试夹具，不得进入生产 registry。"""
    assert "test.synthetic.write" not in {
        s.capability_id for s in StaticCapabilityRegistry().snapshot().specs
    }


def test_every_registered_operation_is_read_only() -> None:
    """M6a PR 1 仍只交付只读闭环：registry 不允许存在写操作。"""
    for spec in StaticCapabilityRegistry().snapshot().specs:
        for operation in spec.operations:
            assert operation.effect_class is EffectClass.READ
            assert operation.side_effect is False


def test_spec_declares_exactly_the_two_m3_operations() -> None:
    assert {op.operation for op in SLOW_QUERY_SPEC.operations} == {OP_LIST, OP_COUNT}
    assert (SLOW_QUERY_SPEC.capability_id, SLOW_QUERY_SPEC.version) == (
        CAPABILITY_ID,
        CAPABILITY_VERSION,
    )
    assert SLOW_QUERY_SPEC.policy_profile == POLICY_PROFILE


def test_prometheus_spec_declares_two_read_operations_and_gateways() -> None:
    assert (PROMETHEUS_ALERT_SPEC.capability_id, PROMETHEUS_ALERT_SPEC.version) == (
        PROMETHEUS_ALERT_CAPABILITY_ID,
        PROMETHEUS_ALERT_CAPABILITY_VERSION,
    )
    assert tuple(
        (operation.operation, operation.gateway)
        for operation in PROMETHEUS_ALERT_SPEC.operations
    ) == (
        ("get_active_alerts", ALERTMANAGER_GATEWAY),
        ("query_metric_range", PROMETHEUS_GATEWAY),
    )


def test_surface_caps_are_the_same_object_as_the_declared_limits() -> None:
    """上限单一真源：surface 的上限与常量必须同源，两处各写一份必然漂移。"""
    assert SLOW_QUERY_SURFACE.max_row_limit == MAX_ROW_LIMIT
    assert SLOW_QUERY_SURFACE.max_window_minutes == MAX_WINDOW_MINUTES


def test_surface_excludes_the_columns_that_carry_free_text() -> None:
    """stmt/digest/clientIp 刻意不在白名单内：它们携带 SQL 原文或不可确定的真实列名。"""
    assert not ({"stmt", "digest", "clientIp"} & set(SLOW_QUERY_SURFACE.allowed_columns))


def test_surface_declares_exactly_one_allowed_table() -> None:
    assert SLOW_QUERY_SURFACE.allowed_tables == (
        "starrocks_audit_db__.starrocks_audit_tbl__",
    )


def test_surface_time_column_is_within_the_allowed_columns() -> None:
    """时间列若不在列白名单内，happy path 会被规则 7 自己挡下。"""
    assert SLOW_QUERY_SURFACE.time_column in SLOW_QUERY_SURFACE.allowed_columns

"""F1 target 配置：黑名单规范化、启用开关、上限与展示名唯一（设计 §6）。"""

from typing import Any

import pytest
from pydantic import ValidationError

from xiaowei_agent.capabilities.readonly_query import (
    ReadonlyQueryTargetCatalog,
    readonly_query_config_revision,
)
from xiaowei_agent.contracts import ResourcesConfig, StarRocksResource
from xiaowei_agent.contracts.sql_query import QualifiedRelation, ReadonlyQueryBudget

_BASE: dict[str, Any] = {
    "kind": "starrocks",
    "resource_id": "a" * 32,
    "environment": "test",
    "display_name": "订单库",
    "host": "fe.example.com",
    "port": 9030,
    "database": "app",
    "username": "f1_reader",
    "tls_mode": "verify_identity",
    "enabled": True,
}


def _resource(**overrides: Any) -> StarRocksResource:
    return StarRocksResource.model_validate({**_BASE, **overrides})


def test_existing_resources_default_to_f1_disabled_without_blocklist() -> None:
    resource = _resource()
    assert resource.f1_enabled is False
    assert resource.blocked_relation_names == ()
    assert resource.f1_budget == ReadonlyQueryBudget(
        preview_max_rows=1000,
        preview_max_bytes=20_971_520,
        query_timeout_seconds=180,
    )


def test_blocked_names_are_lowercased_and_sorted() -> None:
    resource = _resource(blocked_relation_names=["Ops.Salary_MV", "app.secret"])
    assert resource.blocked_relation_names == ("app.secret", "ops.salary_mv")


@pytest.mark.parametrize(
    "names",
    [
        ["secret"],
        ["default_catalog.app.secret"],
        ["app."],
        [".secret"],
        ["app.*"],
        ["app.sec%"],
        ["app.sec?"],
        ["app.secret", "APP.Secret"],
        ["app. secret"],
    ],
)
def test_invalid_or_conflicting_blocked_names_fail(names: list[str]) -> None:
    with pytest.raises(ValidationError):
        _resource(blocked_relation_names=names)


def test_budget_accepts_only_admin_lowered_values() -> None:
    lowered = _resource(
        f1_budget={
            "preview_max_rows": 10,
            "preview_max_bytes": 1_048_576,
            "query_timeout_seconds": 5,
        }
    )
    assert lowered.f1_budget.preview_max_rows == 10
    with pytest.raises(ValidationError):
        _resource(
            f1_budget={
                "preview_max_rows": 1001,
                "preview_max_bytes": 1_048_576,
                "query_timeout_seconds": 5,
            }
        )


def test_f1_enabled_display_names_are_unique_per_environment() -> None:
    first = _resource(f1_enabled=True)
    same_name = _resource(resource_id="b" * 32, f1_enabled=True)
    with pytest.raises(ValidationError):
        ResourcesConfig(generation=1, resources=(first, same_name))
    # 另一个环境、或未启用 F1 的同名资源不冲突。
    ResourcesConfig(
        generation=1,
        resources=(first, _resource(resource_id="b" * 32, environment="dev", f1_enabled=True)),
    )
    ResourcesConfig(
        generation=1,
        resources=(first, _resource(resource_id="b" * 32, f1_enabled=False)),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"host": "fe2.example.com"},
        {"port": 9031},
        {"username": "other_reader"},
        {"database": "ops"},
        {"tls_mode": "verify_ca"},
        {"display_name": "订单库2"},
        {"blocked_relation_names": ["app.secret"]},
        {"f1_enabled": False},
        {
            "f1_budget": {
                "preview_max_rows": 999,
                "preview_max_bytes": 20_971_520,
                "query_timeout_seconds": 180,
            }
        },
    ],
)
def test_every_relevant_change_changes_the_config_revision(change: dict[str, Any]) -> None:
    base = _resource(f1_enabled=True)
    assert readonly_query_config_revision(base) == readonly_query_config_revision(
        _resource(f1_enabled=True)
    )
    assert readonly_query_config_revision(base) != readonly_query_config_revision(
        _resource(**{"f1_enabled": True, **change})
    )


def test_catalog_lists_only_enabled_f1_starrocks_in_scope() -> None:
    config = ResourcesConfig(
        generation=1,
        resources=(
            _resource(f1_enabled=True, display_name="乙"),
            _resource(resource_id="b" * 32, f1_enabled=True, display_name="甲"),
            _resource(resource_id="c" * 32, f1_enabled=False, display_name="丙"),
            _resource(resource_id="d" * 32, f1_enabled=True, enabled=False, display_name="丁"),
            _resource(resource_id="e" * 32, f1_enabled=True, environment="dev", display_name="戊"),
        ),
    )
    catalog = ReadonlyQueryTargetCatalog.from_resources(tenant_id="t1", config=config)
    targets = catalog.targets_for(tenant_id="t1", environment_id="test")
    # 按展示名码点排序：确定性，不依赖区域设置。
    assert [target.display_name for target in targets] == sorted(["甲", "乙"])
    assert catalog.targets_for(tenant_id="other", environment_id="test") == ()
    assert catalog.targets_for(tenant_id="t1", environment_id="prod") == ()


def test_catalog_target_carries_policy_inputs() -> None:
    config = ResourcesConfig(
        generation=1,
        resources=(
            _resource(f1_enabled=True, blocked_relation_names=["app.secret"]),
        ),
    )
    catalog = ReadonlyQueryTargetCatalog.from_resources(tenant_id="t1", config=config)
    (target,) = catalog.targets_for(tenant_id="t1", environment_id="test")
    assert target.resource_id == "a" * 32
    assert target.config_revision == readonly_query_config_revision(config.resources[0])
    assert target.default_database == "app"
    assert target.blocked_relations == frozenset(
        {QualifiedRelation(database="app", name="secret")}
    )


def test_empty_catalog_has_no_targets() -> None:
    assert ReadonlyQueryTargetCatalog.empty().targets_for(
        tenant_id="t1", environment_id="test"
    ) == ()

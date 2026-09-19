"""目标解析：从上下文与草案得到 ``ResolvedTarget``。

目标解析是确定性的：同一上下文必须得到同一目标，因此也得到同一
``target_fingerprint``——审批绑定与恢复时的漂移检测都依赖这一点。
"""

import datetime as dt

import pytest

from xiaowei_agent.capabilities.target import (
    KNOWN_ENVIRONMENT_IDS,
    SELECTOR_VERSION,
    TargetResolutionError,
    resolve_target,
)
from xiaowei_agent.contracts import (
    RequestContext,
    TargetRejection,
)
from xiaowei_agent.planning import compute_target_fingerprint
from xiaowei_agent.planning.starrocks.params import SlowQueryParams


def _context(**overrides: str) -> RequestContext:
    base: dict[str, str] = {
        "tenant_id": "dev-local",
        "actor": "alice",
        "environment_id": "dev",
        "trace_id": "0" * 32,
        "policy_revision": "policy-2026-09-01",
    }
    return RequestContext(**(base | overrides))


def _params(**overrides: object) -> SlowQueryParams:
    base: dict[str, object] = {
        "window_start": dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC),
        "window_end": dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC),
        "min_query_time_ms": 10_000,
        "row_limit": 20,
    }
    return SlowQueryParams(
        **(base | overrides)
    )


def test_same_context_yields_identical_fingerprint() -> None:
    first = resolve_target(context=_context(), params=_params())
    second = resolve_target(context=_context(), params=_params())
    assert first == second
    assert compute_target_fingerprint(first) == compute_target_fingerprint(second)


def test_selector_version_is_incremented_for_the_target_directory_change() -> None:
    target = resolve_target(context=_context(environment_id="dev"), params=_params())

    assert SELECTOR_VERSION == "2"
    assert target.selector_version == "2"


def test_tenant_change_changes_the_fingerprint() -> None:
    one = resolve_target(context=_context(tenant_id="dev-local"), params=_params())
    other = resolve_target(context=_context(tenant_id="other-tenant"), params=_params())
    assert compute_target_fingerprint(one) != compute_target_fingerprint(other)


def test_actor_does_not_change_the_fingerprint() -> None:
    """actor 不是目标的一部分：把它算进去会让同一目标随发起人漂移。"""
    alice = resolve_target(context=_context(actor="alice"), params=_params())
    bob = resolve_target(context=_context(actor="bob"), params=_params())
    assert compute_target_fingerprint(alice) == compute_target_fingerprint(bob)


def test_resolved_target_carries_the_declared_provider_and_kind() -> None:
    target = resolve_target(context=_context(), params=_params())
    assert (target.provider, target.resource_kind) == ("starrocks", "cluster")
    assert target.tenant_id == "dev-local"
    assert target.environment_id == "dev"


def test_test_environment_remains_closed_until_one_resource_is_approved() -> None:
    """不能替负责人从两个占位资源中任选其一。"""
    assert "test" in KNOWN_ENVIRONMENT_IDS

    with pytest.raises(TargetResolutionError) as error:
        resolve_target(context=_context(environment_id="test"), params=_params())

    assert error.value.rejection is TargetRejection.AMBIGUOUS_ENVIRONMENT_DIRECTORY


def test_unresolvable_environment_fails_closed() -> None:
    """环境无法解析出唯一值时不得取默认环境（ADR-007 D2）。"""
    with pytest.raises(TargetResolutionError):
        resolve_target(context=_context(environment_id="unknown"), params=_params())


def test_query_params_cannot_override_the_context_environment() -> None:
    """目标环境只来自 RequestContext；查询参数没有环境字段。"""
    target = resolve_target(context=_context(environment_id="dev"), params=_params())
    assert target.environment_id == "dev"


def test_query_filters_cannot_introduce_extra_resource_ids() -> None:
    """查询过滤不得扩大目标：资源 ID 只能来自环境目录。"""
    plain = resolve_target(context=_context(), params=_params())
    polluted = resolve_target(
        context=_context(),
        params=_params(database="sales", user_name="app_user_1", query_id="q1"),
    )
    assert plain.resource_ids == polluted.resource_ids
    assert compute_target_fingerprint(plain) == compute_target_fingerprint(polluted)

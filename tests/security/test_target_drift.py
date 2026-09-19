"""目标漂移：不可信输入不得改变已解析的目标。

目标指纹是审批绑定与恢复校验的承重字段。凡是能让模型输出、用户原文或外部文本
改变指纹的路径，都等于让一份已批准的计划在恢复后指向另一个目标。
"""

import datetime as dt

import pytest

from xiaowei_agent.capabilities.target import (
    TargetRejection,
    TargetResolutionError,
    resolve_target,
)
from xiaowei_agent.contracts import RequestContext
from xiaowei_agent.planning import compute_target_fingerprint
from xiaowei_agent.planning.starrocks.params import SlowQueryParams

pytestmark = pytest.mark.security

CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database", "sales"),
        ("user_name", "root"),
        ("query_id", "q1"),
    ],
)
def test_query_filters_never_reach_the_resolved_target(
    field: str,
    value: str,
) -> None:
    """即使参数过滤变化，目标仍恒为上下文里的环境。"""
    target = resolve_target(context=CONTEXT, params=_params(**{field: value}))
    assert target.environment_id == CONTEXT.environment_id


def test_target_fingerprint_is_invariant_under_query_filter_changes() -> None:
    """把全部过滤字段设值，指纹必须与空过滤时逐字节相同。"""
    clean = compute_target_fingerprint(resolve_target(context=CONTEXT, params=_params()))
    polluted = compute_target_fingerprint(
        resolve_target(
            context=CONTEXT,
            params=_params(database="sales", user_name="root", query_id="q1"),
        )
    )
    assert clean == polluted


def test_unknown_environment_is_refused_with_a_closed_set_rejection() -> None:
    """拒绝原因必须是闭集枚举成员，不得是拼接出来的自由文本。"""
    hostile = CONTEXT.model_copy(update={"environment_id": "prod"})
    with pytest.raises(TargetResolutionError) as err:
        resolve_target(context=hostile, params=_params())
    assert err.value.rejection is TargetRejection.UNKNOWN_ENVIRONMENT


def test_rejection_message_never_echoes_the_environment_id() -> None:
    """被拒的环境标识来自调用方，不得出现在异常文本里。"""
    canary = "canary" + "-env-7731"
    hostile = CONTEXT.model_copy(update={"environment_id": canary})
    with pytest.raises(TargetResolutionError) as err:
        resolve_target(context=hostile, params=_params())
    assert canary not in str(err.value)
    assert canary not in repr(err.value)


def test_production_is_not_in_the_environment_directory() -> None:
    """生产连接当前不授权（ADR-007 D6）：目录里不得存在任何生产环境项。"""
    from xiaowei_agent.capabilities.target import KNOWN_ENVIRONMENT_IDS

    assert not ({"prod", "production", "prd"} & set(KNOWN_ENVIRONMENT_IDS))

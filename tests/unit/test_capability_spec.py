"""能力声明是 E1 分类的唯一来源，声明层就不许自相矛盾。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.capabilities.specs import SLOW_QUERY_SPEC
from xiaowei_agent.contracts import (
    CapabilitySnapshot,
    CapabilitySpec,
    EffectClass,
    OperationSpec,
)


def _op(**overrides: object) -> OperationSpec:
    base: dict[str, object] = {
        "operation": "list_slow_queries",
        "gateway": "starrocks",
        "effect_class": EffectClass.READ,
        "side_effect": False,
        "argument_schema_ref": "schema.v1",
    }
    return OperationSpec(**(base | overrides))


def _spec(**overrides: object) -> CapabilitySpec:
    base: dict[str, object] = {
        "capability_id": "c",
        "version": "1.0.0",
        "domain": "d",
        "input_schema_ref": "input.c.v1",
        "operations": (_op(),),
        "policy_profile": "p",
        "evidence_contract": "e",
        "eval_ref": "v",
    }
    return CapabilitySpec(**(base | overrides))


def test_write_declared_as_readonly_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _op(effect_class=EffectClass.MUTATE_TARGET, side_effect=False)


def test_read_declared_as_side_effecting_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _op(effect_class=EffectClass.READ, side_effect=True)


def test_operation_requires_a_gateway() -> None:
    """没有 adapter 路由归属的 operation 不能进入能力声明。"""
    with pytest.raises(ValidationError):
        OperationSpec(
            operation="list_slow_queries",
            effect_class=EffectClass.READ,
            side_effect=False,
            argument_schema_ref="schema.v1",
        )


def test_operation_accepts_a_nonempty_gateway() -> None:
    operation = _op(gateway="starrocks")
    assert operation.gateway == "starrocks"


@pytest.mark.parametrize("gateway", ["", " starrocks", "starrocks "])
def test_operation_rejects_an_empty_or_padded_gateway(gateway: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _op(gateway=gateway)
    assert caught.value.errors()[0]["type"] == "value_error"


def test_starrocks_declaration_keeps_version_and_explicit_gateway() -> None:
    """首次显式化既有路由，不得无意义改版本或 operation 顺序。"""
    assert SLOW_QUERY_SPEC.version == "1.0.0"
    assert [
        (operation.operation, operation.gateway)
        for operation in SLOW_QUERY_SPEC.operations
    ] == [
        ("list_slow_queries", "starrocks"),
        ("count_queries_in_window", "starrocks"),
    ]


def test_copy_cannot_make_an_operation_spec_self_contradictory() -> None:
    """声明的内部一致性也不能被一次 copy 绕过。"""
    with pytest.raises(ValidationError):
        _op().model_copy(update={"effect_class": EffectClass.MUTATE_TARGET})


def test_capability_requires_at_least_one_operation() -> None:
    with pytest.raises(ValidationError):
        _spec(operations=())


def test_duplicate_operation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(operations=(_op(), _op()))


def test_snapshot_rejects_duplicate_capability_version_pair() -> None:
    """同一 (capability_id, version) 出现两次会让分类派生结果取决于遍历顺序。"""
    with pytest.raises(ValidationError):
        CapabilitySnapshot(snapshot_id="s", specs=(_spec(), _spec()))


def test_snapshot_allows_two_versions_of_the_same_capability() -> None:
    snapshot = CapabilitySnapshot(
        snapshot_id="s", specs=(_spec(), _spec(version="1.1.0"))
    )
    assert len(snapshot.specs) == 2


def test_capability_requires_input_schema_ref() -> None:
    with pytest.raises(ValidationError):
        CapabilitySpec(
            capability_id="c",
            version="1.0.0",
            domain="d",
            operations=(_op(),),
            policy_profile="p",
            evidence_contract="e",
            eval_ref="v",
        )

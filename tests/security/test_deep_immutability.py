"""映射字段必须深不可变，且不接受不可规范化的标量。

Pydantic 的 frozen=True 只挡属性重绑定，挡不住内部 dict 被原地改写。
若不解决，"计划已冻结"就是假的——plan_hash 算完之后仍可改 typed_arguments。
"""

import datetime as dt
import math

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import Contract, FrozenMap, TaskRecord, TaskStatus

pytestmark = pytest.mark.security


class _Sample(Contract):
    data: FrozenMap


def test_mapping_field_cannot_be_mutated_in_place() -> None:
    sample = _Sample(data={"a": 1})
    with pytest.raises(TypeError):
        sample.data["a"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        del sample.data["a"]  # type: ignore[attr-defined]


def test_mutating_the_original_input_does_not_affect_the_contract() -> None:
    """必须是复制后包装，而不是给外部 dict 套一层视图。"""
    source: dict[str, int] = {"a": 1}
    sample = _Sample(data=source)
    source["a"] = 999
    source["b"] = 2
    assert dict(sample.data) == {"a": 1}


def test_attribute_rebinding_is_still_blocked() -> None:
    sample = _Sample(data={"a": 1})
    with pytest.raises(ValidationError):
        sample.data = {"b": 2}  # type: ignore[misc]


def test_nested_mapping_values_are_rejected_by_scalar_maps() -> None:
    """标量映射不接受嵌套容器；嵌套是夹带任意载荷的通道。"""
    with pytest.raises(ValidationError):
        _Sample(data={"a": {"nested": 1}})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_are_rejected_at_construction(bad: float) -> None:
    """必须在构造时拒绝，而不是等到 canonical_json。

    AdapterResponse.payload 与 EvidenceEnvelope.facts 不经过 canonical_json；
    typed_arguments 里的 NaN 则要到准入时刻算 plan_hash 才抛错，那时计划已经
    进了 TaskStore。
    """
    with pytest.raises(ValidationError):
        _Sample(data={"a": bad})


def _task_record(**updates: object) -> TaskRecord:
    values: dict[str, object] = {
        "task_id": "t1",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "actor": "alice",
        "idempotency_key": "idem-1",
        "request_digest": "a" * 64,
        "status": TaskStatus.CREATED,
        "version": 0,
        "created_seq": 1,
        "attempt_number": 0,
        "task_failure_count": 0,
        "next_attempt_at": None,
    }
    values.update(updates)
    return TaskRecord(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("created_seq", True),
        ("created_seq", 0),
        ("attempt_number", True),
        ("attempt_number", -1),
        ("task_failure_count", True),
        ("task_failure_count", -1),
        ("next_attempt_at", dt.datetime(2026, 9, 5)),
    ],
)
def test_task_execution_accounting_fields_are_strict(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        _task_record(**{field: bad})

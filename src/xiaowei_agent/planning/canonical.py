"""规范化 JSON 与三个确定性指纹。

**不做 str() 兜底**：静默字符串化会让两个语义不同的对象产生同一指纹，而指纹的全部
价值就是"不同即不同"。未知类型一律 ``TypeError``。

**NFC 规范化会造成键碰撞，必须抛错而非静默覆盖**：两个不同键规范化后相同，若后者
覆盖前者，两份不同数据就会产生同一指纹。

**``-0.0`` 归一为 ``0.0``**：两者 ``==`` 为真，但 ``json.dumps`` 分别产出
``"-0.0"`` 与 ``"0.0"``，会让两个相等的取值得到不同指纹——与 NFC 未规范化是同一类
不稳定。

**覆盖完备性由映射表承重**：下面四张「模型字段名 → 指纹键名」表既是实现的取键
依据，也是 ``tests/security/test_hash_coverage.py`` 的断言对象。给 DTO 加字段而
不更新映射表，测试立即转红——靠人记得同步是行不通的。
"""

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from xiaowei_agent.contracts import (
    ExecutionPlan,
    PlanBudget,
    PlanStep,
    ResolvedTarget,
    StepCondition,
    ToolCall,
)

PLAN_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "plan_schema_version": "plan_schema_version",
        "capability_id": "capability_id",
        "capability_version": "capability_version",
        "steps": "ordered_steps",
        "policy_profile": "policy_profile",
        "policy_revision": "policy_revision",
        "budget": "budget",
    }
)

STEP_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "step_id": "step_id",
        "operation": "operation",
        "typed_arguments": "typed_arguments",
        "depends_on": "depends_on",
        "side_effect": "side_effect",
        "effect_class": "effect_class",
        "condition": "condition",
    }
)

PLAN_BUDGET_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "max_steps": "max_steps",
        "max_tool_calls": "max_tool_calls",
        "max_model_tokens": "max_model_tokens",
    }
)

STEP_CONDITION_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "kind": "kind",
        "ref_step_id": "ref_step_id",
        "field": "field",
        "threshold": "threshold",
        "expected_result": "expected_result",
    }
)

TARGET_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "tenant_id": "tenant_id",
        "environment_id": "environment_id",
        "provider": "provider",
        "resource_kind": "resource_kind",
        "resource_ids": "resource_ids",
        "selector_version": "selector_version",
    }
)

TOOL_CALL_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gateway": "gateway",
        "operation": "operation",
        "step_id": "step_id",
        "typed_args": "typed_args",
        "timeout_seconds": "timeout_seconds",
        "idempotency_key": "idempotency_key",
    }
)


def _normalise(value: object) -> object:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not canonicalisable")
        return 0.0 if value == 0.0 else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bytes | bytearray):
        raise TypeError("bytes are not canonicalisable")
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise TypeError("canonical mapping keys must be str")
            nkey = unicodedata.normalize("NFC", key)
            if nkey in out:
                # 用序号而不是键本身定位：映射的键是外部文本，回显它会让"拒绝"
                # 变成外泄通道（与 ValidationError 的 loc 泄漏同一条不变量）。
                raise ValueError(
                    f"canonical key #{index} collides with an earlier key after NFC"
                )
            out[nkey] = _normalise(item)
        return out
    if isinstance(value, Sequence):
        return [_normalise(item) for item in value]
    # 不回显类型名：``type(name, bases, dict)`` 能在运行时造任意类名，因此跨信任
    # 边界对象的类名是**调用方可控数据**，不是"类的身份"。canonical_json 的输入
    # 恰恰来自计划与外部载荷。可规范化类型是闭集，调用方自己知道传了什么。
    raise TypeError("value type is not canonicalisable")


def canonical_json(value: object) -> bytes:
    """固定键序、无空白、UTF-8、NFC 的字节串。不可表达的取值一律抛错。"""
    return json.dumps(
        _normalise(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _condition_payload(condition: StepCondition) -> dict[str, Any]:
    key = STEP_CONDITION_FIELD_TO_HASH_KEY
    return {
        key["kind"]: condition.kind.value,
        key["ref_step_id"]: condition.ref_step_id,
        key["field"]: condition.field,
        key["threshold"]: condition.threshold,
        key["expected_result"]: (
            condition.expected_result.value if condition.expected_result is not None else None
        ),
    }


def _budget_payload(budget: PlanBudget) -> dict[str, Any]:
    key = PLAN_BUDGET_FIELD_TO_HASH_KEY
    return {
        key["max_steps"]: budget.max_steps,
        key["max_tool_calls"]: budget.max_tool_calls,
        key["max_model_tokens"]: budget.max_model_tokens,
    }


def _step_payload(step: PlanStep) -> dict[str, Any]:
    key = STEP_FIELD_TO_HASH_KEY
    return {
        key["step_id"]: step.step_id,
        key["operation"]: step.operation,
        key["typed_arguments"]: dict(step.typed_arguments),
        key["depends_on"]: list(step.depends_on),
        key["side_effect"]: step.side_effect,
        key["effect_class"]: step.effect_class.value,
        key["condition"]: _condition_payload(step.condition),
    }


def _plan_payload(plan: ExecutionPlan) -> dict[str, Any]:
    key = PLAN_FIELD_TO_HASH_KEY
    return {
        key["plan_schema_version"]: plan.plan_schema_version,
        key["capability_id"]: plan.capability_id,
        key["capability_version"]: plan.capability_version,
        key["steps"]: [_step_payload(step) for step in plan.steps],
        key["policy_profile"]: plan.policy_profile,
        key["policy_revision"]: plan.policy_revision,
        key["budget"]: _budget_payload(plan.budget),
    }


def _target_payload(target: ResolvedTarget) -> dict[str, Any]:
    key = TARGET_FIELD_TO_HASH_KEY
    return {
        key["tenant_id"]: target.tenant_id,
        key["environment_id"]: target.environment_id,
        key["provider"]: target.provider,
        key["resource_kind"]: target.resource_kind,
        # ResolvedTarget 已在构造时完成 NFC 规范化并拒绝碰撞，此处只需排序。
        key["resource_ids"]: sorted(target.resource_ids),
        key["selector_version"]: target.selector_version,
    }


def _tool_call_payload(call: ToolCall) -> dict[str, Any]:
    key = TOOL_CALL_FIELD_TO_HASH_KEY
    return {
        key["gateway"]: call.gateway,
        key["operation"]: call.operation,
        key["step_id"]: call.step_id,
        key["typed_args"]: dict(call.typed_args),
        key["timeout_seconds"]: call.timeout_seconds,
        key["idempotency_key"]: call.idempotency_key,
    }


def compute_plan_hash(plan: ExecutionPlan) -> str:
    """ARCHITECTURE §7.1 的规范计划摘要。覆盖 ExecutionPlan 与 PlanStep 全部字段。"""
    return hashlib.sha256(canonical_json(_plan_payload(plan))).hexdigest()


def compute_target_fingerprint(target: ResolvedTarget) -> str:
    """ARCHITECTURE §7.2 的目标指纹。"""
    return hashlib.sha256(canonical_json(_target_payload(target))).hexdigest()


def compute_tool_call_hash(call: ToolCall) -> str:
    """调用内容摘要。使准入凭证能证明被执行的正是已准入的那个 ToolCall。"""
    return hashlib.sha256(canonical_json(_tool_call_payload(call))).hexdigest()

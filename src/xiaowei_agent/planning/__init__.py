"""确定性规划：规范化与指纹。本包只依赖 contracts 与标准库。"""

from xiaowei_agent.planning.canonical import (
    PLAN_BUDGET_FIELD_TO_HASH_KEY,
    PLAN_FIELD_TO_HASH_KEY,
    STEP_CONDITION_FIELD_TO_HASH_KEY,
    STEP_FIELD_TO_HASH_KEY,
    TARGET_FIELD_TO_HASH_KEY,
    TOOL_CALL_FIELD_TO_HASH_KEY,
    canonical_json,
    compute_plan_hash,
    compute_target_fingerprint,
    compute_tool_call_hash,
)

__all__ = [
    "PLAN_BUDGET_FIELD_TO_HASH_KEY",
    "PLAN_FIELD_TO_HASH_KEY",
    "STEP_CONDITION_FIELD_TO_HASH_KEY",
    "STEP_FIELD_TO_HASH_KEY",
    "TARGET_FIELD_TO_HASH_KEY",
    "TOOL_CALL_FIELD_TO_HASH_KEY",
    "canonical_json",
    "compute_plan_hash",
    "compute_target_fingerprint",
    "compute_tool_call_hash",
]

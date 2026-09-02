"""能力声明解析与 E1 分类。本包只依赖 contracts 与标准库。"""

from xiaowei_agent.capabilities.effect import (
    SpecResolutionError,
    build_plan_step,
    derive_effect,
    verify_plan_effects,
)
from xiaowei_agent.capabilities.resolver import CapabilityRegistry, CapabilityResolver

__all__ = [
    "CapabilityRegistry",
    "CapabilityResolver",
    "SpecResolutionError",
    "build_plan_step",
    "derive_effect",
    "verify_plan_effects",
]

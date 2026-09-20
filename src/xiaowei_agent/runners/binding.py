"""Runner 所需的 capability 执行依赖窄契约。"""

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from xiaowei_agent.contracts import (
    EvidenceEnvelope,
    ExecutionPlan,
    PlanStep,
    PolicyProfile,
    PromqlSurface,
    ResolvedTarget,
    SqlSurface,
    ToolResult,
)
from xiaowei_agent.planning.disclosure import DisclosureProjectionBinding


class StepEvidenceBuilder(Protocol):
    """用已准入目标把一个步骤结果转换成该 capability 的证据。"""

    def __call__(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        result: ToolResult,
        captured_at: dt.datetime,
    ) -> EvidenceEnvelope: ...


@dataclass(frozen=True)
class CapabilityExecutionBinding:
    """Runner 执行单个 capability 所需的最小依赖集合。"""

    capability_id: str
    capability_version: str
    disclosure: DisclosureProjectionBinding
    policy_profile: PolicyProfile
    sql_surface: SqlSurface | None
    promql_surface: PromqlSurface | None
    evidence_builder: StepEvidenceBuilder


class ExecutionBindingProvider(Protocol):
    """按计划中的精确 capability key 提供执行依赖。"""

    def execution_for(
        self, *, plan: ExecutionPlan
    ) -> CapabilityExecutionBinding: ...

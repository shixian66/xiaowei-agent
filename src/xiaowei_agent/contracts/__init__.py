"""跨模块契约的唯一公开导入面。

本包是分层的叶子：**只允许依赖 ``xiaowei_agent.redaction`` 与标准库**，
不依赖任何业务模块（``log`` / ``config`` / ``trace`` 均不可）。由
``tests/security/test_module_layering.py`` 承重。
"""

from xiaowei_agent.contracts.approval import AdmissionCertificate, ApprovalRequest
from xiaowei_agent.contracts.base import (
    Contract,
    FiniteFloat,
    FrozenMap,
    FrozenStrMap,
    JsonScalar,
    StrictInt,
    StrictStr,
    TraceId,
    frozen_map,
)
from xiaowei_agent.contracts.candidates import Candidate, CandidateSet, Rejection
from xiaowei_agent.contracts.capability import (
    CapabilitySnapshot,
    CapabilitySpec,
    OperationSpec,
)
from xiaowei_agent.contracts.enums import (
    AdapterStatus,
    ApprovalState,
    BindingRejection,
    Channel,
    EffectClass,
    ErrorCategory,
    ExternalInputKind,
    ExternalSource,
    IntentSource,
    PipelineStage,
    RiskLevel,
    StageOutcome,
    StepConditionKind,
    StepResultStatus,
    TaskStatus,
    ToolCallStatus,
    TransitionRejection,
    TrustLevel,
)
from xiaowei_agent.contracts.errors import AgentError
from xiaowei_agent.contracts.external import ExternalContent, content_digest
from xiaowei_agent.contracts.intent import IntentDraft
from xiaowei_agent.contracts.plan import (
    PLAN_SCHEMA_VERSION,
    ExecutionPlan,
    PlanBudget,
    PlanStep,
    StepCondition,
)
from xiaowei_agent.contracts.policy import PolicyDecision, PolicySnapshot
from xiaowei_agent.contracts.request import RequestContext, RequestEnvelope
from xiaowei_agent.contracts.target import ResolvedTarget
from xiaowei_agent.contracts.tool import ToolCall, ToolResult

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "AdapterStatus",
    "AdmissionCertificate",
    "AgentError",
    "ApprovalRequest",
    "ApprovalState",
    "BindingRejection",
    "Candidate",
    "CandidateSet",
    "CapabilitySnapshot",
    "CapabilitySpec",
    "Channel",
    "Contract",
    "EffectClass",
    "ErrorCategory",
    "ExecutionPlan",
    "ExternalContent",
    "ExternalInputKind",
    "ExternalSource",
    "FiniteFloat",
    "FrozenMap",
    "FrozenStrMap",
    "IntentDraft",
    "IntentSource",
    "JsonScalar",
    "OperationSpec",
    "PipelineStage",
    "PlanBudget",
    "PlanStep",
    "PolicyDecision",
    "PolicySnapshot",
    "Rejection",
    "RequestContext",
    "RequestEnvelope",
    "ResolvedTarget",
    "RiskLevel",
    "StageOutcome",
    "StepCondition",
    "StepConditionKind",
    "StepResultStatus",
    "StrictInt",
    "StrictStr",
    "TaskStatus",
    "ToolCall",
    "ToolCallStatus",
    "ToolResult",
    "TraceId",
    "TransitionRejection",
    "TrustLevel",
    "content_digest",
    "frozen_map",
]

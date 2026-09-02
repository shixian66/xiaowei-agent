"""跨模块契约的唯一公开导入面。

本包是分层的叶子：**只允许依赖 ``xiaowei_agent.redaction`` 与标准库**，
不依赖任何业务模块（``log`` / ``config`` / ``trace`` 均不可）。由
``tests/security/test_module_layering.py`` 承重。
"""

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
from xiaowei_agent.contracts.request import RequestContext, RequestEnvelope

__all__ = [
    "AdapterStatus",
    "AgentError",
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
    "Rejection",
    "RequestContext",
    "RequestEnvelope",
    "RiskLevel",
    "StageOutcome",
    "StepConditionKind",
    "StepResultStatus",
    "StrictInt",
    "StrictStr",
    "TaskStatus",
    "ToolCallStatus",
    "TraceId",
    "TransitionRejection",
    "TrustLevel",
    "content_digest",
    "frozen_map",
]

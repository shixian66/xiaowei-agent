"""跨契约的闭集枚举，全项目共享的唯一定义处。

**一次性全量定义**是刻意的：按任务增量新增枚举会让下游任务引用到尚未定义的
成员。枚举没有实现成本，集中定义可消除整类顺序错误。

**闭集本身就是安全边界**：:class:`EffectClass` 不设 ``UNKNOWN`` 成员，因此
"分类未知"在解析层即失败，不存在一个可被当作只读放行的取值
（ADR-007 D7 fail-closed 规则 1）。
"""

from enum import StrEnum


class ExternalSource(StrEnum):
    TOOL = "tool"
    KNOWLEDGE = "knowledge"
    WEB = "web"
    USER = "user"
    LOG = "log"


class TrustLevel(StrEnum):
    """只有一个成员：外部内容永远不可信。"""

    UNTRUSTED = "untrusted"


class EffectClass(StrEnum):
    """不设 UNKNOWN：未声明即校验失败。"""

    READ = "read"
    MUTATE_TARGET = "mutate_target"


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    POLICY = "policy"
    APPROVAL = "approval"
    TARGET = "target"
    UPSTREAM = "upstream"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    INTERNAL = "internal"


class Channel(StrEnum):
    API = "api"
    CLI = "cli"
    WEB = "web"
    FEISHU = "feishu"


class IntentSource(StrEnum):
    MODEL = "model"
    USER = "user"
    FAKE = "fake"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalState(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class StepConditionKind(StrEnum):
    ALWAYS = "always"
    EVIDENCE_FIELD_ABSENT = "evidence_field_absent"
    EVIDENCE_ROW_COUNT_BELOW = "evidence_row_count_below"
    PRIOR_STEP_RESULT_IS = "prior_step_result_is"


class StepResultStatus(StrEnum):
    """**步骤**结果，与 :class:`TaskStatus`（**任务**状态）分属两个层级。

    两者混用会让"某个步骤失败"与"整个任务失败"在条件里不可区分。
    """

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"
    INDETERMINATE = "indeterminate"


class TransitionRejection(StrEnum):
    VERSION_MISMATCH = "version_mismatch"
    ILLEGAL_TRANSITION = "illegal_transition"
    TERMINAL_PROTECTED = "terminal_protected"
    # S105 是误报：这是拒绝原因的枚举取值，不是凭证。
    STALE_FENCING_TOKEN = "stale_fencing_token"  # noqa: S105
    LEASE_NOT_HELD = "lease_not_held"


class AdapterStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


class ToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    INDETERMINATE = "indeterminate"


class PipelineStage(StrEnum):
    """与 ARCHITECTURE §13.1 的错误归因阶段逐一对应。"""

    INTENT = "intent"
    RESOLVER = "resolver"
    PLANNER = "planner"
    ADMISSION = "admission"
    GATEWAY = "gateway"
    EVIDENCE = "evidence"
    REFLECTION = "reflection"
    RENDERING = "rendering"
    LIFECYCLE = "lifecycle"


class StageOutcome(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExternalInputKind(StrEnum):
    APPROVAL_DECISION = "approval_decision"
    USER_SUPPLEMENT = "user_supplement"


class BindingRejection(StrEnum):
    PLAN_DRIFT = "plan_drift"
    TARGET_DRIFT = "target_drift"
    POLICY_REVISION_DRIFT = "policy_revision_drift"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_NOT_GRANTED = "approval_not_granted"

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

    **取值刻意与 TaskStatus 不相交**（``step_`` 前缀）。只改名不改值是不够的：
    StrEnum 成员就是字符串，若两边都用 ``"failed"``，在 pydantic 的 lax 模式下把
    ``TaskStatus.FAILED`` 传给步骤条件会被静默接受——"某个步骤失败"与"整个任务
    失败"于是在数据层就不可区分。用 strict 拒绝跨枚举传值则会打断 M4 从
    TaskStore 反序列化时的 ``str -> StrEnum``，因此在**取值**上分开。
    """

    OK = "step_ok"
    FAILED = "step_failed"
    TIMEOUT = "step_timeout"
    SKIPPED = "step_skipped"


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


class AttemptIntent(StrEnum):
    DISPATCH = "dispatch"
    APPROVAL_RESUME = "approval_resume"


class TaskAttemptRejection(StrEnum):
    LIVE_LEASE = "live_lease"
    NOT_DISPATCHABLE = "not_dispatchable"
    RETRY_NOT_DUE = "retry_not_due"
    TERMINAL_PROTECTED = "terminal_protected"
    RETRY_EXHAUSTED = "retry_exhausted"
    SUBMISSION_INVARIANT_VIOLATION = "submission_invariant_violation"


class GrantRejection(StrEnum):
    TERMINAL_PROTECTED = "terminal_protected"
    STATUS_NOT_ALLOWED = "status_not_allowed"
    LEASE_NOT_HELD = "lease_not_held"
    STALE_FENCING = "stale_fencing"


class RetryReason(StrEnum):
    UNCLASSIFIED_ERROR = "unclassified_error"


class RetryDecision(StrEnum):
    SCHEDULED = "scheduled"
    ALREADY_SCHEDULED = "already_scheduled"
    COMMAND_MISMATCH = "command_mismatch"
    STALE_FENCING = "stale_fencing"
    TERMINAL_PROTECTED = "terminal_protected"


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


class PolicyReason(StrEnum):
    """ToolPolicy 判定原因的闭集。

    ``ALLOWED`` 之外的每一个成员都是一条**拒绝**理由。把理由写成闭集而不是自由
    文本，使审计里的原因可以被聚合、被断言，也使新增一种放行路径必须改枚举并
    过评审。
    """

    ALLOWED = "policy.allowed"
    PROFILE_MISMATCH = "policy.profile_mismatch"
    OPERATION_NOT_ALLOWED = "policy.operation_not_allowed"
    EFFECT_CLASS_NOT_ALLOWED = "policy.effect_class_not_allowed"
    ENVIRONMENT_NOT_ALLOWED = "policy.environment_not_allowed"
    TIMEOUT_EXCEEDS_PROFILE = "policy.timeout_exceeds_profile"
    TENANT_MISMATCH = "policy.tenant_mismatch"


class SqlGuardRejection(StrEnum):
    """SQL AST 校验失败的闭集原因。

    每个成员对应 SQLGuard 的一条规则。拒绝码是**审计里看到的原因**，因此规则
    顺序决定了它有多具体：最宽泛的 ``RECOMPILE_MISMATCH`` 排在最后，否则它会
    吞掉所有更具体的原因，AST 规则也随之成为不可达的死代码。
    """

    UNKNOWN_TEMPLATE = "unknown_template"
    UNKNOWN_DIALECT = "unknown_dialect"
    AMBIGUOUS_CHARACTER = "ambiguous_character"
    UNPARSABLE = "unparsable"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NON_SELECT = "non_select"
    FORBIDDEN_NODE = "forbidden_node"
    TABLE_NOT_ALLOWED = "table_not_allowed"
    COLUMN_NOT_ALLOWED = "column_not_allowed"
    STAR_NOT_ALLOWED = "star_not_allowed"
    COMMENT_PRESENT = "comment_present"
    LIMIT_MISSING = "limit_missing"
    LIMIT_EXCEEDED = "limit_exceeded"
    WINDOW_UNBOUNDED = "window_unbounded"
    WINDOW_MISMATCH = "window_mismatch"
    WINDOW_TOO_WIDE = "window_too_wide"
    RECOMPILE_MISMATCH = "recompile_mismatch"


class TargetRejection(StrEnum):
    """目标解析失败的闭集原因。

    不设 ``UNKNOWN`` 兜底成员：解析不出唯一目标时必须给出具体原因，而不是用一个
    可被当作"大概没事"的取值把 fail-closed 稀释掉。
    """

    UNKNOWN_ENVIRONMENT = "unknown_environment"
    EMPTY_ENVIRONMENT_DIRECTORY = "empty_environment_directory"


class BindingRejection(StrEnum):
    PLAN_DRIFT = "plan_drift"
    TARGET_DRIFT = "target_drift"
    POLICY_REVISION_DRIFT = "policy_revision_drift"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_NOT_GRANTED = "approval_not_granted"

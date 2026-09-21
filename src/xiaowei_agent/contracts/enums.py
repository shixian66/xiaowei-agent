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


class ReadClass(StrEnum):
    """只读操作的静态读取范围分类。"""

    BOUNDED = "bounded"
    RESTRICTED = "restricted"


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


class ChannelPermission(StrEnum):
    """M7 渠道层真正消费的权限闭集。"""

    VIEW_SAFE_TASK = "view_safe_task"
    SUBMIT_READONLY_TASK = "submit_readonly_task"
    ADMIN_ALL_SAFE_TASKS = "admin_all_safe_tasks"


class IdentitySource(StrEnum):
    FEISHU = "feishu"
    LOCAL_ADMIN = "local_admin"


class ProductRole(StrEnum):
    """身份目录里的作用域角色闭集（规格 §6.1）。

    不设 UNKNOWN 兜底成员：解析不出角色时必须失败，而不是落到一个可被当作
    "大概是普通用户"的取值上——那正是 fail-closed 被稀释的典型形状。
    """

    ADMIN = "admin"
    OPERATOR = "operator"
    USER = "user"


class UserStatus(StrEnum):
    """账号状态闭集。禁用是一个**状态**，不是删除行。"""

    ACTIVE = "active"
    DISABLED = "disabled"


class AdminCapability(StrEnum):
    """管理面能力闭集，七成员逐字取自 ADR-013 修订。

    与 :class:`ChannelPermission` **分开建模**是刻意的：渠道权限闭集被 M7 的投影
    断言承重，往里加管理面成员会让那些断言全部失去意义。

    ``MANAGE_INTEGRATIONS`` 与 ``RUN_CONNECTION_TESTS`` 只允许 ``LOCAL_ADMIN``
    认证来源，这条不写在枚举里，而由
    :func:`xiaowei_agent.governance.product_roles.admin_capabilities` 强制。
    """

    MANAGE_USERS = "manage_users"
    MANAGE_DUTY_BINDINGS = "manage_duty_bindings"
    VIEW_ADMIN_AUDIT = "view_admin_audit"
    VIEW_PRIVATE_TASK_CONTENT = "view_private_task_content"
    VIEW_INTEGRATION_STATUS = "view_integration_status"
    MANAGE_INTEGRATIONS = "manage_integrations"
    RUN_CONNECTION_TESTS = "run_connection_tests"


class AdminAuditAction(StrEnum):
    """管理面审计动作闭集。

    **认证生命周期不在这里**：登录成功/失败、改密、退出走结构化安全日志。
    把它们混进来会让审计表同时承担两种保留期与两种读者，而改密改的是凭据、
    不是授权。``test_authentication_lifecycle_never_became_an_audit_action``
    钉住这一点。
    """

    USER_CREATED = "user_created"
    USER_STATUS_CHANGED = "user_status_changed"
    ROLE_ASSIGNED = "role_assigned"
    ROLE_REVOKED = "role_revoked"
    EXTERNAL_IDENTITY_BOUND = "external_identity_bound"
    EXTERNAL_IDENTITY_UNBOUND = "external_identity_unbound"
    LOCAL_ADMIN_BOOTSTRAPPED = "local_admin_bootstrapped"
    LEGACY_IDENTITY_MIGRATED = "legacy_identity_migrated"


class AdminAuditTargetKind(StrEnum):
    """审计目标类别闭集（规格 §14.2）。

    ``ACTIVATION`` / ``DUTY_BINDING`` / ``CONFIG`` / ``TASK_CONTENT`` 在 W1a 没有
    任何命令能产生，但它们是规格逐字列出的**值域**，留在闭集里与"提前建一张没有
    消费者的表"是两回事。
    """

    USER = "user"
    ACTIVATION = "activation"
    DUTY_BINDING = "duty_binding"
    CONFIG = "config"
    TASK_CONTENT = "task_content"


class AdminAuditOutcome(StrEnum):
    """审计结果闭集。

    ``STARTED`` 属于规格 §14.2 的两阶段配置审计，W1a 的目录动作一律单阶段——
    这条差别由契约与数据库 CHECK 同时强制。
    """

    STARTED = "started"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"


class AdminAuditReasonCode(StrEnum):
    """负面结果的原因码闭集。

    写成闭集而不是自由文本，使原因可以被聚合、被断言；也使审计表不必为了解释
    一次拒绝而开一个能容纳异常正文的 ``str`` 通道。
    """

    ACTOR_NOT_ADMIN = "actor_not_admin"
    AUTH_SOURCE_NOT_ALLOWED = "auth_source_not_allowed"
    TARGET_NOT_FOUND = "target_not_found"
    SCOPE_MISMATCH = "scope_mismatch"
    CONFLICT = "conflict"
    AUDIT_UNWRITABLE = "audit_unwritable"


class ProviderName(StrEnum):
    """`integrations.json` 里可配置的 Provider 闭集。"""

    GEMINI = "gemini"
    FEISHU = "feishu"


class WebMode(StrEnum):
    """Web 对外暴露方式。决定 public origin 允许的协议与主机形态。

    ``LAN_HTTP`` 是 RI5 本地管理面的部署形态：仅局域网、仅 http、仅 IP 字面量。
    ``HTTPS`` 是既有的对外形态，沿用既有 HTTPS origin 的全部约束。
    """

    LAN_HTTP = "lan_http"
    HTTPS = "https"


class ChannelKind(StrEnum):
    FEISHU_PRIVATE = "feishu_private"
    FEISHU_GROUP = "feishu_group"
    WEB = "web"


class DestinationKind(StrEnum):
    FEISHU_MESSAGE_CARD = "feishu_message_card"
    FEISHU_PRIVATE_NOTICE = "feishu_private_notice"


class ProjectionState(StrEnum):
    PENDING_INITIAL = "pending_initial"
    WAITING_TERMINAL = "waiting_terminal"
    DELIVERING_TERMINAL = "delivering_terminal"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class ProjectionErrorCode(StrEnum):
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"
    PROVIDER_FORBIDDEN = "provider_forbidden"
    PROVIDER_INVALID_PAYLOAD = "provider_invalid_payload"
    PROVIDER_INTERNAL = "provider_internal"


class IntentSource(StrEnum):
    MODEL = "model"
    USER = "user"
    FAKE = "fake"


class InteractionSource(StrEnum):
    MODEL = "model"
    RULE = "rule"
    FAKE = "fake"


class InteractionKind(StrEnum):
    CONVERSATION = "conversation"
    KNOWLEDGE_LOOKUP = "knowledge_lookup"
    LOG_ANALYSIS = "log_analysis"
    CAPABILITY_REQUEST = "capability_request"
    UNKNOWN = "unknown"


class RoutingDisposition(StrEnum):
    PROCEED = "proceed"
    RESPOND = "respond"
    CLARIFY = "clarify"
    REFUSE = "refuse"


class ClarificationField(StrEnum):
    TIME_RANGE = "time_range"
    DATABASE = "database"
    USER_NAME = "user_name"
    QUERY_ID = "query_id"
    ALERT_NAME = "alert_name"
    INSTANCE = "instance"
    FINGERPRINT = "fingerprint"
    ASSET_ID = "asset_id"
    HOSTNAME = "hostname"
    IP = "ip"


class ClarificationReasonCode(StrEnum):
    INTERACTION_KIND_AMBIGUOUS = "interaction.kind_ambiguous"
    INTERACTION_ENVIRONMENT_ASSERTION_UNCLEAR = (
        "interaction.environment_assertion_unclear"
    )
    CAPABILITY_FIELDS_MISSING = "capability.fields_missing"
    CAPABILITY_FIELDS_AMBIGUOUS = "capability.fields_ambiguous"
    CAPABILITY_ASSET_SELECTOR_REQUIRED = "capability.asset_selector_required"


class InteractionRejectionReasonCode(StrEnum):
    ROUTE_NOT_AVAILABLE = "interaction.route_not_available"
    ENVIRONMENT_CONTEXT_MISMATCH = "interaction.environment_context_mismatch"
    CAPABILITY_DRAFT_FORBIDDEN = "interaction.capability_draft_forbidden"
    CAPABILITY_DRAFT_MISSING = "interaction.capability_draft_missing"
    CLARIFICATION_SUBJECT_INCOMPATIBLE = (
        "interaction.clarification_subject_incompatible"
    )
    CAPABILITY_FIELDS_INVALID = "capability.fields_invalid"


class ModelErrorCode(StrEnum):
    """供应商错误只允许映射为这些本地安全码。"""

    CREDENTIAL_UNAVAILABLE = "model_credential_unavailable"
    AMBIENT_PROXY = "model_ambient_proxy"
    UNAUTHORIZED = "model_unauthorized"
    FORBIDDEN = "model_forbidden"
    RATE_LIMITED = "model_rate_limited"
    SERVER_ERROR = "model_server_error"
    TRANSPORT_ERROR = "model_transport_error"
    TIMEOUT = "model_timeout"
    INVALID_RESPONSE = "model_invalid_response"
    UNAVAILABLE = "model_unavailable"


class ModelCallKind(StrEnum):
    """模型 trace 的调用种类闭集。"""

    INTENT = "intent"
    INTERACTION = "interaction"
    ADVISORY = "advisory"


class ModelFallbackCode(StrEnum):
    """模型没有产出可用结果时的安全、可聚合原因。"""

    DISABLED = "model_disabled"
    INPUT_REJECTED = "model_input_rejected"
    APPLICATION_TIMEOUT = "model_application_timeout"
    CREDENTIAL_UNAVAILABLE = "model_credential_unavailable"
    AMBIENT_PROXY = "model_ambient_proxy"
    UNAUTHORIZED = "model_unauthorized"
    FORBIDDEN = "model_forbidden"
    RATE_LIMITED = "model_rate_limited"
    SERVER_ERROR = "model_server_error"
    TRANSPORT_ERROR = "model_transport_error"
    PROVIDER_TIMEOUT = "model_timeout"
    INVALID_RESPONSE = "model_invalid_response"
    UNAVAILABLE = "model_unavailable"


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
    CLARIFICATION_REQUIRED = "clarification_required"
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


class StepAttemptDecision(StrEnum):
    PROCEED = "proceed"
    ALREADY_COMMITTED = "already_committed"
    STALE_FENCING = "stale_fencing"
    NOT_RUNNABLE = "not_runnable"
    UNKNOWN_STEP = "unknown_step"
    BUDGET_EXHAUSTED = "budget_exhausted"


class StepOutcomeKind(StrEnum):
    TOOL_RESULT = "tool_result"
    MALFORMED_ADAPTER = "malformed_adapter"


class StepCommitRejection(StrEnum):
    STALE_FENCING = "stale_fencing"
    NOT_RUNNABLE = "not_runnable"
    ALREADY_COMMITTED_DIFFERENT = "already_committed_different"
    NO_ATTEMPT_IN_FLIGHT = "no_attempt_in_flight"


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
    MODEL = "model"
    RESOLVER = "resolver"
    PLANNER = "planner"
    DISCLOSURE = "disclosure"
    ADMISSION = "admission"
    GATEWAY = "gateway"
    EVIDENCE = "evidence"
    REFLECTION = "reflection"
    RENDERING = "rendering"
    LIFECYCLE = "lifecycle"


class ExecutionDisclosureDisposition(StrEnum):
    """计划级执行披露分类。"""

    BOUNDED_READ = "bounded_read"
    RESTRICTED_READ = "restricted_read"
    SIDE_EFFECT = "side_effect"


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
    READ_CLASS_NOT_ALLOWED = "policy.read_class_not_allowed"
    ENVIRONMENT_MISMATCH = "policy.environment_mismatch"
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


class PromqlGuardRejection(StrEnum):
    """固定模板 PromQL 准入失败的闭集原因。"""

    UNKNOWN_TEMPLATE = "unknown_template"
    INVALID_ARGUMENTS = "invalid_arguments"
    RECOMPILE_MISMATCH = "recompile_mismatch"
    INCOMPLETE_ENVELOPE = "incomplete_envelope"
    ENVELOPE_CONFLICT = "envelope_conflict"
    SURFACE_MISSING = "surface_missing"


class TargetRejection(StrEnum):
    """目标解析失败的闭集原因。

    不设 ``UNKNOWN`` 兜底成员：解析不出唯一目标时必须给出具体原因，而不是用一个
    可被当作"大概没事"的取值把 fail-closed 稀释掉。
    """

    UNKNOWN_ENVIRONMENT = "unknown_environment"
    EMPTY_ENVIRONMENT_DIRECTORY = "empty_environment_directory"
    AMBIGUOUS_ENVIRONMENT_DIRECTORY = "ambiguous_environment_directory"


class BindingRejection(StrEnum):
    PLAN_DRIFT = "plan_drift"
    TARGET_DRIFT = "target_drift"
    POLICY_REVISION_DRIFT = "policy_revision_drift"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_NOT_GRANTED = "approval_not_granted"

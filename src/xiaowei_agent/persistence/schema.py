"""PostgreSQL 表结构：SQLAlchemy Core 的 ``Table`` 定义。

``tasks``、步骤 journal 与渠道表为并发判定、索引和 fencing 展开必要字段；其余
append-only 契约载荷主要以 JSONB 保存。任何展开字段都必须由 schema/迁移对齐测试守住，
避免契约形状在第二处静默漂移。

**用 Core 不用 ORM**：ORM 的 identity map 与 flush 时机会让"必须采纳存储层 winner"
这条不变量更难断言，而并发语义正是 M4 的全部承重点。

**所有映射到严格字符串域的列一律 ``Text``，不用 ``UUID``。** ``task_id`` 用 ``uuid``
列读回的是 ``UUID`` 对象，而 ``TaskRecord.task_id`` 是 ``TaskId``，strict 模式会直接
拒绝——写 DDL 时"主键当然用 uuid"的直觉在这里恰好是错的。

本模块是**迁移之外的第二处 schema 表述**，因此必然有漂移风险。对价是
``tests/contract/test_schema_matches_migration.py``：把两边分别编译成 PostgreSQL
方言的 DDL 再逐字比较，离线执行，不需要数据库。
"""

from collections.abc import Iterable
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from xiaowei_agent.contracts.activation import ActivationSource, ActivationStatus
from xiaowei_agent.contracts.admin_audit import (
    DIRECTORY_ACTIONS,
    ROLE_EFFECT_ACTIONS,
    STATUS_EFFECT_ACTIONS,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.integration_config import ConfigDomain

_DIRECTORY_ACTION_VALUES: Final[frozenset[str]] = frozenset(
    action.value for action in DIRECTORY_ACTIONS
)
_ROLE_EFFECT_VALUES: Final[frozenset[str]] = frozenset(
    action.value for action in ROLE_EFFECT_ACTIONS
)
_STATUS_EFFECT_VALUES: Final[frozenset[str]] = frozenset(
    action.value for action in STATUS_EFFECT_ACTIONS
)
_NEGATIVE_OUTCOME_VALUES: Final[frozenset[str]] = frozenset(
    {AdminAuditOutcome.DENIED.value, AdminAuditOutcome.FAILED.value}
)
_TERMINAL_OUTCOME_VALUES: Final[frozenset[str]] = _NEGATIVE_OUTCOME_VALUES | {
    AdminAuditOutcome.SUCCEEDED.value
}
"""三个终态。**不是**只有 ``succeeded``：偏唯一索引的谓词只写 ``= 'succeeded'`` 时，
一次操作可以同时留下一条 succeeded 和一条 failed，而读者无从判断哪条是真的。"""

METADATA: Final = sa.MetaData()

FENCING_SEQUENCE_NAME: Final = "task_fencing_token_seq"
"""fencing token 的单调来源。

用 SEQUENCE 而不是"当前最大值 + 1"：后者需要读-改-写，并发下要么加锁要么出现重复
token，而重复 token 会让整条 fencing 规则失效。序列天然单调且无需额外加锁；跳号
无害——契约只要求"旧持有者的 token < 当前 token"，不要求连续。
"""

FENCING_SEQUENCE: Final = sa.Sequence(FENCING_SEQUENCE_NAME, start=1)

CREATED_SEQUENCE_NAME: Final = "task_created_seq"
"""任务创建顺序的数据库分配源；只承诺稳定的近似公平，不承诺 commit FIFO。"""

CREATED_SEQUENCE: Final = sa.Sequence(CREATED_SEQUENCE_NAME, start=1)

PROJECTION_FENCING_SEQUENCE_NAME: Final = "projection_fencing_token_seq"
"""渠道投影 claim 的独立单调 fencing 来源。"""

PROJECTION_FENCING_SEQUENCE: Final = sa.Sequence(
    PROJECTION_FENCING_SEQUENCE_NAME, start=1
)

TASKS: Final = sa.Table(
    "tasks",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("environment_id", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("request_digest", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_seq", sa.BigInteger, CREATED_SEQUENCE, nullable=False),
    sa.Column("attempt_number", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("task_failure_count", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("retry_scheduled_by_attempt", sa.BigInteger, nullable=True),
    sa.Column("retry_command_digest", sa.Text, nullable=True),
    sa.Column("idempotency_scope_digest", sa.CHAR(64), nullable=False),
    sa.Column("terminal_reason", sa.Text, nullable=True),
    sa.Column("lease_owner", sa.Text, nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("fencing_token", sa.BigInteger, nullable=True),
    # 幂等作用域。全局键表会让跨租户同键共用一个任务，
    # test_idempotency_key_is_scoped_per_tenant 承重。
    sa.UniqueConstraint("created_seq", name="uq_tasks_created_seq"),
    sa.UniqueConstraint(
        "idempotency_scope_digest", name="uq_tasks_idempotency_scope_digest"
    ),
    sa.CheckConstraint("version >= 0", name="ck_tasks_version_non_negative"),
    sa.CheckConstraint("created_seq > 0", name="ck_tasks_created_seq_positive"),
    sa.CheckConstraint("attempt_number >= 0", name="ck_tasks_attempt_number_non_negative"),
    sa.CheckConstraint(
        "task_failure_count >= 0", name="ck_tasks_task_failure_count_non_negative"
    ),
    sa.CheckConstraint(
        "(retry_scheduled_by_attempt IS NULL AND retry_command_digest IS NULL)"
        " OR (retry_scheduled_by_attempt IS NOT NULL AND retry_command_digest IS NOT NULL)",
        name="ck_tasks_retry_markers_consistent",
    ),
    sa.CheckConstraint(
        "retry_scheduled_by_attempt IS NULL"
        " OR (retry_scheduled_by_attempt > 0"
        " AND retry_scheduled_by_attempt <= attempt_number)",
        name="ck_tasks_retry_attempt_not_future",
    ),
    sa.CheckConstraint(
        "fencing_token IS NULL OR fencing_token > 0", name="ck_tasks_fencing_token_positive"
    ),
    # 与 TaskRecord._lease_fields_are_consistent 同一不变量，两层独立表达。任一字段
    # 单独存在都意味着状态机中间态泄漏到了存储里，此时"是否持有租约"没有确定答案。
    sa.CheckConstraint(
        "(lease_owner IS NULL AND lease_expires_at IS NULL AND fencing_token IS NULL)"
        " OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL"
        " AND fencing_token IS NOT NULL)",
        name="ck_tasks_lease_fields_consistent",
    ),
)
"""任务事实真源。

**租约过期后三个字段不清空。** ``fencing_token IS NOT NULL`` 正是"任务曾被租出"的
判据，而整条 fencing 闭合规则锚定在这一点上（见 ``persistence/decisions.py``）。清空
它会重新打开"过期后不带 token 即可写入"那个缺口。
"""

TASK_SUBMISSIONS: Final = sa.Table(
    "task_submissions",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("envelope", JSONB, nullable=False),
    sa.Column("context", JSONB, nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("submission_digest", sa.CHAR(64), nullable=False),
    sa.Column("clarification_parent_task_id", sa.Text, nullable=True),
    sa.ForeignKeyConstraint(
        ["clarification_parent_task_id"],
        ["tasks.task_id"],
        name="fk_task_submissions_clarification_parent_task",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "clarification_parent_task_id",
        name="uq_task_submissions_clarification_parent_task_id",
    ),
)
"""不可变提交事实；终态 M4 历史任务是唯一允许缺少该行的任务。"""

TASK_STEP_EXECUTIONS: Final = sa.Table(
    "task_step_executions",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("step_id", sa.Text, primary_key=True),
    sa.Column("attempt_count", sa.BigInteger, nullable=False),
    sa.Column("last_fencing_token", sa.BigInteger, nullable=False),
    sa.Column("result_status", sa.Text, nullable=True),
    sa.Column("kind", sa.Text, nullable=True),
    sa.Column("evidence_id", sa.Text, nullable=True),
    sa.Column("commit_digest", sa.CHAR(64), nullable=True),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "attempt_count > 0", name="ck_task_steps_attempt_count_positive"
    ),
    sa.CheckConstraint(
        "last_fencing_token > 0", name="ck_task_steps_fencing_token_positive"
    ),
    sa.CheckConstraint(
        "(result_status IS NULL AND kind IS NULL AND commit_digest IS NULL"
        " AND committed_at IS NULL) OR (result_status IS NOT NULL"
        " AND kind IS NOT NULL AND commit_digest IS NOT NULL"
        " AND committed_at IS NOT NULL)",
        name="ck_task_steps_terminal_fields_consistent",
    ),
    sa.CheckConstraint(
        "(evidence_id IS NOT NULL AND kind = 'tool_result')"
        " OR (evidence_id IS NULL AND kind IS DISTINCT FROM 'tool_result')",
        name="ck_task_steps_evidence_kind_consistent",
    ),
    sa.CheckConstraint(
        "result_status IS NULL OR "
        "(kind = 'tool_result' AND result_status IN ('step_ok', 'step_failed',"
        " 'step_timeout') AND evidence_id IS NOT NULL) OR "
        "(kind = 'malformed_adapter' AND result_status = 'step_failed'"
        " AND evidence_id IS NULL)",
        name="ck_task_steps_result_shape",
    ),
)
"""每个计划步骤的 in-flight 或不可变终局；缺行表示该步骤从未开始。"""

TASK_PLANS: Final = sa.Table(
    "task_plans",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("plan", JSONB, nullable=False),
    sa.Column("target", JSONB, nullable=False),
)
"""``StoredPlan``：恰两列载荷，与 ``test_plan_store_holds_nothing_but_plan_and_target``
钉死的字段集一致。

**不存 ``plan_hash`` / ``target_fingerprint``**（见 ``schema`` 的非目标说明）。
"""

TASK_EVIDENCE: Final = sa.Table(
    "task_evidence",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("evidence_id", sa.Text, primary_key=True),
    # 每任务内的序号。读回契约是"按写入顺序返回"，而 JSONB 里的 captured_at 可能
    # 同值——用时间戳排序会让同毫秒写入的两条证据顺序不确定。
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("envelope", JSONB, nullable=False),
    sa.UniqueConstraint("task_id", "seq", name="uq_task_evidence_seq"),
)
"""append-only 证据台账。"""

TASK_INTERACTION_ARTIFACTS: Final = sa.Table(
    "task_interaction_artifacts",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("artifact_version", sa.Integer, nullable=False),
    sa.Column("draft", JSONB, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=True),
    sa.Column("model", sa.Text, nullable=True),
    sa.Column("provider_origin", sa.Text, nullable=True),
    sa.Column("prompt_revision", sa.Text, nullable=False),
    sa.Column("schema_revision", sa.Text, nullable=False),
    sa.Column("input_digest", sa.CHAR(64), nullable=False),
    sa.Column("result_digest", sa.CHAR(64), nullable=False),
    sa.Column("usage", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fencing_token", sa.BigInteger, nullable=False),
    sa.ForeignKeyConstraint(
        ["task_id"],
        ["tasks.task_id"],
        name="fk_task_interaction_artifacts_task",
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(
        "artifact_version IN (1, 2)", name="ck_task_interaction_artifacts_version"
    ),
    sa.CheckConstraint(
        "fencing_token > 0", name="ck_task_interaction_artifacts_fencing_positive"
    ),
    sa.CheckConstraint(
        "(origin = 'model' AND provider IS NOT NULL AND model IS NOT NULL"
        " AND provider_origin IS NOT NULL) OR"
        " (origin = 'rule' AND provider IS NULL AND model IS NULL"
        " AND provider_origin IS NULL)",
        name="ck_task_interaction_artifacts_origin_identity",
    ),
)
"""Router 前已接受的 model/rule interaction；每个 task insert-once。"""

TASK_MODEL_ADVISORIES: Final = sa.Table(
    "task_model_advisories",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("artifact_version", sa.Integer, nullable=False),
    sa.Column("advisory", JSONB, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("provider_origin", sa.Text, nullable=False),
    sa.Column("prompt_revision", sa.Text, nullable=False),
    sa.Column("schema_revision", sa.Text, nullable=False),
    sa.Column("input_digest", sa.CHAR(64), nullable=False),
    sa.Column("result_digest", sa.CHAR(64), nullable=False),
    sa.Column("usage", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fencing_token", sa.BigInteger, nullable=False),
    sa.ForeignKeyConstraint(
        ["task_id"], ["tasks.task_id"], name="fk_task_model_advisories_task", ondelete="CASCADE"
    ),
    sa.CheckConstraint(
        "artifact_version = 1", name="ck_task_model_advisories_version"
    ),
    sa.CheckConstraint(
        "origin = 'model'", name="ck_task_model_advisories_origin"
    ),
    sa.CheckConstraint(
        "fencing_token > 0", name="ck_task_model_advisories_fencing_positive"
    ),
)
"""终态前已接受的慢查询 advisory；每个 task insert-once。"""

TASK_CLARIFICATION_RECORDS: Final = sa.Table(
    "task_clarification_records",
    METADATA,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("record_version", sa.Integer, nullable=False),
    sa.Column("subject", JSONB, nullable=False),
    sa.Column("reason_code", sa.Text, nullable=False),
    sa.Column("missing_fields", JSONB, nullable=False),
    sa.Column("confirmed_slots", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fencing_token", sa.BigInteger, nullable=False),
    sa.ForeignKeyConstraint(
        ["task_id"],
        ["tasks.task_id"],
        name="fk_task_clarification_records_task",
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(
        "record_version = 1", name="ck_task_clarification_records_version"
    ),
    sa.CheckConstraint(
        "fencing_token > 0", name="ck_task_clarification_records_fencing_positive"
    ),
)
"""I1 澄清事实；每个 task insert-once。"""

TASK_APPROVALS: Final = sa.Table(
    "task_approvals",
    METADATA,
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("step_id", sa.Text, nullable=False),
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("request", JSONB, nullable=False),
    sa.PrimaryKeyConstraint("task_id", "seq", name="pk_task_approvals"),
)
"""append-only 审批记录。

主键是 ``(task_id, seq)`` 而不是 ``(task_id, step_id)``：同一步骤可以被多次请求审批
（超时后重新发起），把 ``step_id`` 放进主键会让第二次请求覆盖第一次的记录，而审批
历史正是事后追责要看的东西。
"""

TASK_AUDIT_EVENTS: Final = sa.Table(
    "task_audit_events",
    METADATA,
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("event", JSONB, nullable=False),
    sa.PrimaryKeyConstraint("task_id", "seq", name="pk_task_audit_events"),
)
"""append-only 审计事件。**证据表不能替代审计**：证据回答"看到了什么"，审计回答
"系统做了什么、准入判成了什么"，一条被策略拒绝的调用不产生证据但必须留下审计。

载荷是 M2 已定义的 ``TraceEvent``，其 ``detail`` 在契约层已做键与值双向脱敏并限长，
因此审计表不会成为新的脱敏缺口——不需要在持久化层再写一份脱敏。
"""

CHANNEL_BINDINGS: Final = sa.Table(
    "channel_bindings",
    METADATA,
    sa.Column("binding_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("environment_id", sa.Text, nullable=False),
    sa.Column("channel", sa.Text, nullable=False),
    sa.Column("initiator_subject_ref", sa.Text, nullable=False),
    sa.Column("conversation_ref", sa.Text, nullable=True),
    sa.Column("source_event_ref", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["task_id"],
        ["tasks.task_id"],
        name="fk_channel_bindings_task",
        ondelete="CASCADE",
    ),
    sa.UniqueConstraint("task_id", name="uq_channel_bindings_task"),
    sa.UniqueConstraint(
        "tenant_id",
        "environment_id",
        "channel",
        "source_event_ref",
        name="uq_channel_bindings_source",
    ),
    sa.CheckConstraint(
        "channel IN ('feishu_private', 'feishu_group', 'web')",
        name="ck_channel_bindings_channel",
    ),
    sa.CheckConstraint(
        "channel != 'feishu_group' OR conversation_ref IS NOT NULL",
        name="ck_channel_bindings_group_conversation",
    ),
)
"""渠道来源绑定；只保存授权所需引用，不复制任务状态或请求正文。"""

PROJECTION_SUBSCRIPTIONS: Final = sa.Table(
    "projection_subscriptions",
    METADATA,
    sa.Column("subscription_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("destination_kind", sa.Text, nullable=False),
    sa.Column("destination_ref", sa.Text, nullable=False),
    sa.Column("source_message_ref", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("last_projected_task_version", sa.BigInteger, nullable=True),
    sa.Column("attempt_number", sa.BigInteger, nullable=False),
    sa.Column("claim_owner", sa.Text, nullable=True),
    sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("fencing_token", sa.BigInteger, nullable=True),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provider_failure_count", sa.BigInteger, nullable=False),
    sa.Column("last_error_code", sa.Text, nullable=True),
    sa.Column("payload_digest", sa.CHAR(64), nullable=True),
    sa.ForeignKeyConstraint(
        ["task_id"],
        ["tasks.task_id"],
        name="fk_projection_subscriptions_task",
        ondelete="CASCADE",
    ),
    sa.UniqueConstraint(
        "task_id",
        "destination_kind",
        "destination_ref",
        name="uq_projection_subscriptions_destination",
    ),
    sa.CheckConstraint(
        "destination_kind IN ('feishu_message_card', 'feishu_private_notice')",
        name="ck_projection_subscriptions_destination_kind",
    ),
    sa.CheckConstraint(
        "state IN ('pending_initial', 'waiting_terminal', 'delivering_terminal',"
        " 'completed', 'dead_letter')",
        name="ck_projection_subscriptions_state",
    ),
    sa.CheckConstraint(
        "attempt_number >= 0",
        name="ck_projection_subscriptions_attempt_non_negative",
    ),
    sa.CheckConstraint(
        "provider_failure_count >= 0",
        name="ck_projection_subscriptions_failure_non_negative",
    ),
    sa.CheckConstraint(
        "fencing_token IS NULL OR fencing_token > 0",
        name="ck_projection_subscriptions_fencing_positive",
    ),
    sa.CheckConstraint(
        "last_projected_task_version IS NULL OR last_projected_task_version >= 0",
        name="ck_projection_subscriptions_version_non_negative",
    ),
    sa.CheckConstraint(
        "(claim_owner IS NULL AND claim_expires_at IS NULL AND fencing_token IS NULL)"
        " OR (claim_owner IS NOT NULL AND claim_expires_at IS NOT NULL"
        " AND fencing_token IS NOT NULL)",
        name="ck_projection_subscriptions_claim_consistent",
    ),
    sa.CheckConstraint(
        "state NOT IN ('completed', 'dead_letter') OR"
        " (claim_owner IS NULL AND claim_expires_at IS NULL AND fencing_token IS NULL)",
        name="ck_projection_subscriptions_terminal_unclaimed",
    ),
    sa.CheckConstraint(
        "(last_projected_task_version IS NULL AND payload_digest IS NULL) OR"
        " (last_projected_task_version IS NOT NULL AND payload_digest IS NOT NULL)",
        name="ck_projection_subscriptions_projection_consistent",
    ),
    sa.CheckConstraint(
        "(provider_failure_count = 0 AND last_error_code IS NULL) OR"
        " (provider_failure_count > 0 AND last_error_code IS NOT NULL)",
        name="ck_projection_subscriptions_failure_consistent",
    ),
    sa.CheckConstraint(
        "last_error_code IS NULL OR last_error_code IN"
        " ('provider_rate_limited', 'provider_timeout', 'provider_unavailable',"
        " 'provider_unauthorized', 'provider_forbidden', 'provider_invalid_payload',"
        " 'provider_internal')",
        name="ck_projection_subscriptions_error_code",
    ),
    sa.CheckConstraint(
        "state != 'completed' OR"
        " (source_message_ref IS NOT NULL AND last_projected_task_version IS NOT NULL"
        " AND payload_digest IS NOT NULL)",
        name="ck_projection_subscriptions_completed_payload",
    ),
)
sa.Index(
    "ix_projection_subscriptions_due",
    PROJECTION_SUBSCRIPTIONS.c.state,
    PROJECTION_SUBSCRIPTIONS.c.next_attempt_at,
    PROJECTION_SUBSCRIPTIONS.c.subscription_id,
)
"""渠道投影投递状态；任务当前真相必须回读 ``tasks``。"""

WEB_OAUTH_STATES: Final = sa.Table(
    "web_oauth_states",
    METADATA,
    sa.Column("state_digest", sa.CHAR(64), primary_key=True),
    sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "expires_at > issued_at",
        name="ck_web_oauth_states_expiry_after_issue",
    ),
    sa.CheckConstraint(
        "consumed_at IS NULL OR"
        " (consumed_at >= issued_at AND consumed_at < expires_at)",
        name="ck_web_oauth_states_consumption_window",
    ),
)
"""OAuth state 的单次消费事实；只保存不可逆摘要。"""

WEB_OAUTH_LOGIN_CONTEXTS: Final = sa.Table(
    "web_oauth_login_contexts",
    METADATA,
    sa.Column("state_digest", sa.CHAR(64), primary_key=True),
    sa.Column("return_intent_kind", sa.Text, nullable=False),
    sa.Column("return_intent_task_id", sa.Text, nullable=True),
    sa.Column("return_intent_request_id", sa.Text, nullable=True),
    sa.ForeignKeyConstraint(
        ["state_digest"],
        ["web_oauth_states.state_digest"],
        name="fk_web_oauth_login_contexts_state_digest",
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(
        "return_intent_kind IN"
        " ('activation_status', 'admin_center', 'safe_task_detail', 'workbench')",
        name="ck_web_oauth_login_contexts_kind_closed",
    ),
    sa.CheckConstraint(
        "(return_intent_kind IN ('workbench', 'admin_center')"
        " AND return_intent_task_id IS NULL"
        " AND return_intent_request_id IS NULL)"
        " OR (return_intent_kind = 'safe_task_detail'"
        " AND return_intent_task_id IS NOT NULL"
        " AND return_intent_request_id IS NULL)"
        " OR (return_intent_kind = 'activation_status'"
        " AND return_intent_task_id IS NULL"
        " AND return_intent_request_id IS NOT NULL)",
        name="ck_web_oauth_login_contexts_intent_shape",
    ),
)
"""登录域 state 的闭集返回意图；state 收割时由外键级联删除。"""

WEB_OAUTH_TEST_CONTEXTS: Final = sa.Table(
    "web_oauth_test_contexts",
    METADATA,
    sa.Column("state_digest", sa.CHAR(64), primary_key=True),
    sa.Column("operation_id", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ["state_digest"],
        ["web_oauth_states.state_digest"],
        name="fk_web_oauth_test_contexts_state_digest",
        ondelete="CASCADE",
    ),
    sa.UniqueConstraint("operation_id", name="uq_web_oauth_test_contexts_operation_id"),
    sa.CheckConstraint(
        "left(operation_id, 3) = 'w4:' AND char_length(operation_id) <= 64",
        name="ck_web_oauth_test_contexts_operation_id_shape",
    ),
)
"""W4a OAuth 连接测试 state 与其审计 operation id；state 收割时由外键级联删除。

与 ``web_oauth_login_contexts`` 是两张表：登录分支只认登录 context，测试分支只认测试
context，两者互不回退。只保存 state 摘要与服务端派生的 ``w4:`` operation id。
"""

WEB_SESSIONS: Final = sa.Table(
    "web_sessions",
    METADATA,
    sa.Column("session_digest", sa.CHAR(64), primary_key=True),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("auth_source", sa.Text, nullable=False),
    sa.Column("public_origin_digest", sa.CHAR(64), nullable=False),
    sa.CheckConstraint(
        "expires_at > issued_at",
        name="ck_web_sessions_expiry_after_issue",
    ),
    sa.CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= issued_at",
        name="ck_web_sessions_revocation_after_issue",
    ),
    sa.CheckConstraint(
        "auth_source IN ('local_admin', 'feishu')",
        name="ck_web_sessions_auth_source_closed",
    ),
)
"""浏览器 session；不保存 cookie 明文、权限或租户环境快照。

``public_origin_digest`` 把 session 绑在签发它的 public origin 上：换模式或换
origin 之后旧 session 必然对不上，等价于全体登出，而不是在新 origin 下继续有效。
"""

LOCAL_ADMINS: Final = sa.Table(
    "local_admins",
    METADATA,
    sa.Column("id", sa.SmallInteger, primary_key=True, autoincrement=False),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("must_change_password", sa.Boolean, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "user_id",
        sa.Text,
        sa.ForeignKey(
            "user_accounts.user_id",
            name="fk_local_admins_user_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    ),
    sa.CheckConstraint("id = 1", name="ck_local_admins_single_row"),
)
"""本地管理员；CHECK 把表锁成最多一行，不存在"第二个管理员"这种状态。"""

def _closed_set(column: str, values: Iterable[str]) -> str:
    """把一个闭集枚举渲染成 CHECK 的 ``IN`` 列表。

    从枚举生成而不是手抄：手抄的那份不会随枚举增长，于是新增一个成员会在数据库
    层被拒绝，而契约层照常放行——两层给出不同答案时，症状是一条写入在真实库上
    失败、在内存实现上成功。

    ``sorted`` 是为了 DDL 稳定：集合迭代序会变，而 ``schema.py`` 与迁移必须逐字
    相等（``tests/contract/test_schema_matches_migration.py``）。
    """
    listed = ", ".join(f"'{value}'" for value in sorted(values))
    return f"{column} IN ({listed})"


def _nullable_closed_set(column: str, values: Iterable[str]) -> str:
    return f"{column} IS NULL OR {_closed_set(column, values)}"


SERVICE_CONFIG_STATE: Final = sa.Table(
    "service_config_state",
    METADATA,
    sa.Column("service_name", sa.Text, primary_key=True),
    sa.Column("config_domain", sa.Text, primary_key=True),
    sa.Column("loaded_generation", sa.Integer, nullable=False),
    sa.Column("load_status", sa.Text, nullable=False),
    sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "loaded_generation > 0",
        name="ck_service_config_state_generation_positive",
    ),
    sa.CheckConstraint(
        "load_status IN ('loaded', 'invalid')",
        name="ck_service_config_state_status_closed",
    ),
    sa.CheckConstraint(
        _closed_set("config_domain", (member.value for member in ConfigDomain)),
        name="ck_service_config_state_domain_closed",
    ),
)
"""各进程的加载回执，按 ``(service_name, config_domain)`` 键控（W4a ``rev_0018``）。

``config_domain`` 的闭集一次接纳 ``ai/feishu/resources`` 三域：W4b 不必只为扩一个
闭集再造 migration。

``invalid`` 表示"读到了这一代但没读成"。它与"缺回执"一样落到 PENDING_RESTART，
不产生第六个页面状态；该列只供页面显示原因提示。文件缺失或整体损坏时读不出
generation，此时**不写行**——``loaded_generation > 0`` 的 CHECK 意味着硬凑一个
代次就是伪造证据。
"""

PROVIDER_TEST_STATE: Final = sa.Table(
    "provider_test_state",
    METADATA,
    sa.Column("check_name", sa.Text, primary_key=True),
    sa.Column("tested_generation", sa.Integer, nullable=False),
    sa.Column("test_status", sa.Text, nullable=False),
    sa.Column("tested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=False),
    sa.Column("error_code", sa.Text, nullable=True),
    sa.Column("error_message", sa.Text, nullable=True),
    sa.CheckConstraint(
        "check_name IN ('gemini_connection', 'feishu_credentials', 'feishu_oauth')",
        name="ck_provider_test_state_check_name_closed",
    ),
    sa.CheckConstraint(
        "tested_generation > 0",
        name="ck_provider_test_state_generation_positive",
    ),
    sa.CheckConstraint(
        "test_status IN ('passed', 'failed')",
        name="ck_provider_test_state_status_closed",
    ),
    sa.CheckConstraint(
        "duration_ms >= 0 AND duration_ms <= 600000",
        name="ck_provider_test_state_duration_bounded",
    ),
    sa.CheckConstraint(
        "(test_status = 'passed') = (error_code IS NULL)",
        name="ck_provider_test_state_error_matches_status",
    ),
)
"""控制面探针结果；通过与错误码互斥由 CHECK 保证，不靠调用方自觉。"""

USER_ACCOUNTS: Final = sa.Table(
    "user_accounts",
    METADATA,
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("actor", name="uq_user_accounts_actor"),
    sa.CheckConstraint(
        _closed_set("status", (member.value for member in UserStatus)),
        name="ck_user_accounts_status_closed",
    ),
)
"""身份目录的账号。

``actor`` **全局唯一**，作用域不在这里——这是与规格 §6.1 字段表的有意偏离，且
严格强于规格要求的"同一作用域内拒绝 actor 冲突"。不唯一时，同一个人可以在两个
账号下各拿一套角色，而撤销其中一个不影响另一个。
"""

USER_ROLE_ASSIGNMENTS: Final = sa.Table(
    "user_role_assignments",
    METADATA,
    sa.Column(
        "user_id",
        sa.Text,
        sa.ForeignKey(
            "user_accounts.user_id",
            name="fk_user_role_assignments_user_id",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    ),
    sa.Column("tenant_id", sa.Text, primary_key=True),
    sa.Column("environment_id", sa.Text, primary_key=True),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        _closed_set("role", (member.value for member in ProductRole)),
        name="ck_user_role_assignments_role_closed",
    ),
)
"""某个账号在某个作用域里的角色；主键是三元组，一个作用域最多一个角色。

外键是 ``RESTRICT`` 而不是 ``CASCADE``：``CASCADE`` 会让一次删账号静默带走它的
角色行，而那正是事后要查"当时这个人有什么权限"时唯一的依据。
"""

USER_ROLE_ASSIGNMENTS_SCOPE_USER_INDEX: Final = sa.Index(
    "ix_user_role_assignments_scope_user",
    USER_ROLE_ASSIGNMENTS.c.tenant_id,
    USER_ROLE_ASSIGNMENTS.c.environment_id,
    USER_ROLE_ASSIGNMENTS.c.user_id,
)

EXTERNAL_IDENTITIES: Final = sa.Table(
    "external_identities",
    METADATA,
    sa.Column("provider", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, primary_key=True),
    sa.Column("environment_id", sa.Text, primary_key=True),
    sa.Column("subject_ref_digest", sa.CHAR(64), primary_key=True),
    sa.Column(
        "user_id",
        sa.Text,
        sa.ForeignKey(
            "user_accounts.user_id",
            name="fk_external_identities_user_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "provider",
        "tenant_id",
        "environment_id",
        "user_id",
        name="uq_external_identities_one_subject_per_account",
    ),
    sa.CheckConstraint(
        _closed_set("provider", (IdentitySource.FEISHU.value,)),
        name="ck_external_identities_provider_closed",
    ),
)
"""外部主体到账号的绑定。**只存摘要**。

``open_id`` 落库就等于给它开了一条同时进入备份、慢查询日志和运维截图的通道。

两个方向都钉住：主键保证"一个外部主体最多绑一个账号"，UNIQUE 保证"一个账号在
同作用域最多绑一个主体"。只钉主键时，一个账号可以绑上任意多个 open_id，于是
"撤销这个人"要撤几次没有定论。
"""

ACTIVATION_REQUESTS: Final = sa.Table(
    "activation_requests",
    METADATA,
    sa.Column("request_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("environment_id", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("subject_ref_digest", sa.CHAR(64), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("return_intent_kind", sa.Text, nullable=True),
    sa.Column("return_intent_task_id", sa.Text, nullable=True),
    sa.Column("return_intent_request_id", sa.Text, nullable=True),
    sa.Column("source_event_digest", sa.CHAR(64), nullable=True),
    sa.Column("source_chat_digest", sa.CHAR(64), nullable=True),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("decided_by", sa.Text, nullable=True),
    sa.Column("approved_role", sa.Text, nullable=True),
    sa.CheckConstraint(
        _closed_set("provider", (IdentitySource.FEISHU.value,)),
        name="ck_activation_requests_provider_closed",
    ),
    sa.CheckConstraint(
        _closed_set("source", (member.value for member in ActivationSource)),
        name="ck_activation_requests_source_closed",
    ),
    sa.CheckConstraint(
        _closed_set("status", (member.value for member in ActivationStatus)),
        name="ck_activation_requests_status_closed",
    ),
    sa.CheckConstraint(
        _nullable_closed_set(
            "approved_role", (ProductRole.USER.value, ProductRole.OPERATOR.value)
        ),
        name="ck_activation_requests_approved_role_closed",
    ),
    sa.CheckConstraint(
        "expires_at > requested_at",
        name="ck_activation_requests_expiration_after_request",
    ),
    sa.CheckConstraint(
        "(source IN ('web_login', 'safe_task_link')"
        " AND source_event_digest IS NULL AND source_chat_digest IS NULL)"
        " OR (source = 'feishu_group' AND source_event_digest IS NOT NULL"
        " AND source_chat_digest IS NOT NULL)",
        name="ck_activation_requests_source_digests_match",
    ),
    sa.CheckConstraint(
        "(return_intent_kind IS NULL AND return_intent_task_id IS NULL"
        " AND return_intent_request_id IS NULL)"
        " OR (return_intent_kind IN ('workbench', 'admin_center')"
        " AND return_intent_task_id IS NULL"
        " AND return_intent_request_id IS NULL)"
        " OR (return_intent_kind = 'safe_task_detail'"
        " AND return_intent_task_id IS NOT NULL"
        " AND return_intent_request_id IS NULL)"
        " OR (return_intent_kind = 'activation_status'"
        " AND return_intent_task_id IS NULL"
        " AND return_intent_request_id IS NOT NULL)",
        name="ck_activation_requests_return_intent_shape",
    ),
    sa.CheckConstraint(
        "(source = 'feishu_group' AND return_intent_kind IS NULL)"
        " OR (source = 'safe_task_link'"
        " AND return_intent_kind = 'safe_task_detail')"
        " OR (source = 'web_login'"
        " AND return_intent_kind IN"
        " ('workbench', 'admin_center', 'activation_status'))",
        name="ck_activation_requests_source_intent_match",
    ),
    sa.CheckConstraint(
        "(status IN ('pending', 'expired') AND decided_at IS NULL"
        " AND decided_by IS NULL AND approved_role IS NULL)"
        " OR (status = 'rejected' AND decided_at IS NOT NULL"
        " AND decided_by IS NOT NULL AND approved_role IS NULL)"
        " OR (status = 'approved' AND decided_at IS NOT NULL"
        " AND decided_by IS NOT NULL AND approved_role IS NOT NULL)",
        name="ck_activation_requests_decision_fields_match_status",
    ),
)
"""未知飞书主体的短期激活事实；事件与群引用只保存独立摘要。"""

ACTIVATION_PENDING_SUBJECT_INDEX: Final = sa.Index(
    "uq_activation_requests_pending_subject",
    ACTIVATION_REQUESTS.c.tenant_id,
    ACTIVATION_REQUESTS.c.environment_id,
    ACTIVATION_REQUESTS.c.provider,
    ACTIVATION_REQUESTS.c.subject_ref_digest,
    unique=True,
    postgresql_where=ACTIVATION_REQUESTS.c.status == ActivationStatus.PENDING.value,
)
"""同一作用域和外部主体最多一个有效待办。"""

ACTIVATION_SCOPE_STATUS_REQUESTED_INDEX: Final = sa.Index(
    "ix_activation_requests_scope_status_requested",
    ACTIVATION_REQUESTS.c.tenant_id,
    ACTIVATION_REQUESTS.c.environment_id,
    ACTIVATION_REQUESTS.c.status,
    ACTIVATION_REQUESTS.c.requested_at.desc(),
    ACTIVATION_REQUESTS.c.request_id.desc(),
)

ADMIN_AUDIT_EVENTS: Final = sa.Table(
    "admin_audit_events",
    METADATA,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("operation_id", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("environment_id", sa.Text, nullable=False),
    sa.Column("actor_user_id", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("auth_source", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target_kind", sa.Text, nullable=False),
    sa.Column("target_ref_digest", sa.CHAR(64), nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("reason_code", sa.Text, nullable=True),
    sa.Column("effect_role", sa.Text, nullable=True),
    sa.Column("effect_status", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        _closed_set("auth_source", (member.value for member in IdentitySource)),
        name="ck_admin_audit_events_auth_source_closed",
    ),
    sa.CheckConstraint(
        _closed_set("action", (member.value for member in AdminAuditAction)),
        name="ck_admin_audit_events_action_closed",
    ),
    sa.CheckConstraint(
        _closed_set("target_kind", (member.value for member in AdminAuditTargetKind)),
        name="ck_admin_audit_events_target_kind_closed",
    ),
    sa.CheckConstraint(
        _closed_set("outcome", (member.value for member in AdminAuditOutcome)),
        name="ck_admin_audit_events_outcome_closed",
    ),
    sa.CheckConstraint(
        _nullable_closed_set(
            "reason_code", (member.value for member in AdminAuditReasonCode)
        ),
        name="ck_admin_audit_events_reason_code_closed",
    ),
    sa.CheckConstraint(
        _nullable_closed_set("effect_role", (member.value for member in ProductRole)),
        name="ck_admin_audit_events_effect_role_closed",
    ),
    sa.CheckConstraint(
        _nullable_closed_set("effect_status", (member.value for member in UserStatus)),
        name="ck_admin_audit_events_effect_status_closed",
    ),
    sa.CheckConstraint(
        f"({_closed_set('outcome', _NEGATIVE_OUTCOME_VALUES)})"
        " = (reason_code IS NOT NULL)",
        name="ck_admin_audit_events_reason_code_matches_outcome",
    ),
    sa.CheckConstraint(
        "outcome = 'succeeded'"
        " OR (effect_role IS NULL AND effect_status IS NULL)",
        name="ck_admin_audit_events_effect_only_on_success",
    ),
    sa.CheckConstraint(
        "outcome <> 'succeeded' OR"
        f" ({_closed_set('action', _ROLE_EFFECT_VALUES)})"
        " = (effect_role IS NOT NULL)",
        name="ck_admin_audit_events_role_effect_required",
    ),
    sa.CheckConstraint(
        "outcome <> 'succeeded' OR"
        f" ({_closed_set('action', _STATUS_EFFECT_VALUES)})"
        " = (effect_status IS NOT NULL)",
        name="ck_admin_audit_events_status_effect_required",
    ),
    sa.CheckConstraint(
        "outcome <> 'started' OR"
        f" NOT ({_closed_set('action', _DIRECTORY_ACTION_VALUES)})",
        name="ck_admin_audit_events_directory_actions_are_single_phase",
    ),
)
"""Admin 审计事件；append-only，**没有任何自由文本列**。

只要留一个能容纳异常正文的列，第一个赶工的调用方就会把 ``str(exc)`` 塞进去，而
那时它已经进了备份。原因一律走闭集 ``reason_code``，结果一律走两个闭集 effect 列。

effect 与 action、``reason_code`` 与 ``outcome`` 都用 ``=`` 写成**双向**绑定而不是
单向蕴含：只钉一个方向时，"成功却带着原因码"与"改了角色却不记是哪个角色"都能过。
"""

ADMIN_AUDIT_EVENTS_CREATED_AT_INDEX: Final = sa.Index(
    "ix_admin_audit_events_created_at",
    ADMIN_AUDIT_EVENTS.c.created_at,
)

ADMIN_AUDIT_SCOPE_CREATED_INDEX: Final = sa.Index(
    "ix_admin_audit_events_scope_created",
    ADMIN_AUDIT_EVENTS.c.tenant_id,
    ADMIN_AUDIT_EVENTS.c.environment_id,
    ADMIN_AUDIT_EVENTS.c.created_at.desc(),
    ADMIN_AUDIT_EVENTS.c.event_id.desc(),
)

ADMIN_AUDIT_ONE_STARTED_PER_OPERATION: Final = sa.Index(
    "uq_admin_audit_one_started_per_operation",
    ADMIN_AUDIT_EVENTS.c.operation_id,
    unique=True,
    postgresql_where=ADMIN_AUDIT_EVENTS.c.outcome == AdminAuditOutcome.STARTED.value,
)
"""一次操作最多一条 ``STARTED``。

用**偏**唯一索引而不是给 ``operation_id`` 加全局 UNIQUE：规格 §14.2 的两阶段配置
审计要求同一个 operation 写 ``STARTED`` 与终态两条事件，全局唯一会让第二条必然
撞约束；换一个 operation id 又无法证明两条事件属于同一次操作。
"""

ADMIN_AUDIT_ONE_TERMINAL_PER_OPERATION: Final = sa.Index(
    "uq_admin_audit_one_terminal_per_operation",
    ADMIN_AUDIT_EVENTS.c.operation_id,
    unique=True,
    postgresql_where=ADMIN_AUDIT_EVENTS.c.outcome.in_(
        sorted(_TERMINAL_OUTCOME_VALUES)
    ),
)
"""一次操作最多一条终态事件。

谓词必须列出**三个**终态：只写 ``= 'succeeded'`` 时，一次操作可以同时留下一条
succeeded 和一条 failed，而读者无从判断哪条是真的。
"""

ALL_TABLES: Final = (
    TASKS,
    TASK_SUBMISSIONS,
    TASK_STEP_EXECUTIONS,
    TASK_PLANS,
    TASK_EVIDENCE,
    TASK_INTERACTION_ARTIFACTS,
    TASK_MODEL_ADVISORIES,
    TASK_CLARIFICATION_RECORDS,
    TASK_APPROVALS,
    TASK_AUDIT_EVENTS,
    CHANNEL_BINDINGS,
    PROJECTION_SUBSCRIPTIONS,
    WEB_OAUTH_STATES,
    WEB_OAUTH_LOGIN_CONTEXTS,
    WEB_OAUTH_TEST_CONTEXTS,
    WEB_SESSIONS,
    LOCAL_ADMINS,
    SERVICE_CONFIG_STATE,
    PROVIDER_TEST_STATE,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
    EXTERNAL_IDENTITIES,
    ACTIVATION_REQUESTS,
    ADMIN_AUDIT_EVENTS,
)

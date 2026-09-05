"""PostgreSQL 表结构：SQLAlchemy Core 的 ``Table`` 定义。

**只有 ``tasks`` 把契约字段展开成列**，因为 CAS、租约判定和幂等唯一性都要在 SQL 里
作用于单个字段。其余四张表以 JSONB 存 DTO，只提取查询与唯一性所需的列——展开一个
不需要被 SQL 检索的字段，等于把契约形状复制到第二处，两份迟早漂移。

**用 Core 不用 ORM**：ORM 的 identity map 与 flush 时机会让"必须采纳存储层 winner"
这条不变量更难断言，而并发语义正是 M4 的全部承重点。

**所有映射到 ``StrictStr`` 的列一律 ``Text``，不用 ``UUID``。** ``task_id`` 用 ``uuid``
列读回的是 ``UUID`` 对象，而 ``TaskRecord.task_id`` 是 ``StrictStr``，strict 模式会
直接拒绝——写 DDL 时"主键当然用 uuid"的直觉在这里恰好是错的。

本模块是**迁移之外的第二处 schema 表述**，因此必然有漂移风险。对价是
``tests/contract/test_schema_matches_migration.py``：把两边分别编译成 PostgreSQL
方言的 DDL 再逐字比较，离线执行，不需要数据库。
"""

from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

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

ALL_TABLES: Final = (
    TASKS,
    TASK_SUBMISSIONS,
    TASK_STEP_EXECUTIONS,
    TASK_PLANS,
    TASK_EVIDENCE,
    TASK_APPROVALS,
    TASK_AUDIT_EVENTS,
)

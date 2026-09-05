"""M5 task execution columns, created order, and fixed-width idempotency scope.

Revision ID: 0002_task_execution_columns
Revises: 0001_initial
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_task_execution_columns"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATED_SEQUENCE = "task_created_seq"


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence(_CREATED_SEQUENCE, start=1)))
    op.add_column("tasks", sa.Column("created_seq", sa.BigInteger(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("attempt_number", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("task_failure_count", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "tasks", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tasks", sa.Column("idempotency_scope_digest", sa.CHAR(length=64), nullable=True)
    )

    op.execute(
        sa.text(
            "WITH ranked AS ("
            " SELECT task_id, row_number() OVER (ORDER BY task_id) AS created_seq FROM tasks"
            ") UPDATE tasks SET created_seq = ranked.created_seq"
            " FROM ranked WHERE tasks.task_id = ranked.task_id"
        )
    )
    op.execute(
        sa.text(
            "SELECT setval('task_created_seq', "
            "COALESCE((SELECT max(created_seq) FROM tasks), 0) + 1, false)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET idempotency_scope_digest = encode(sha256(convert_to("
            "'{\"environment_id\":' || to_json(environment_id)::text || "
            "',\"idempotency_key\":' || to_json(idempotency_key)::text || "
            "',\"tenant_id\":' || to_json(tenant_id)::text || '}', 'UTF8')), 'hex')"
        )
    )

    op.alter_column(
        "tasks",
        "created_seq",
        nullable=False,
        server_default=sa.text("nextval('task_created_seq')"),
    )
    op.alter_column("tasks", "idempotency_scope_digest", nullable=False)
    op.create_check_constraint("ck_tasks_created_seq_positive", "tasks", "created_seq > 0")
    op.create_check_constraint(
        "ck_tasks_attempt_number_non_negative", "tasks", "attempt_number >= 0"
    )
    op.create_check_constraint(
        "ck_tasks_task_failure_count_non_negative", "tasks", "task_failure_count >= 0"
    )
    op.create_unique_constraint("uq_tasks_created_seq", "tasks", ["created_seq"])
    op.drop_constraint("uq_tasks_idempotency_scope", "tasks", type_="unique")
    op.create_unique_constraint(
        "uq_tasks_idempotency_scope_digest", "tasks", ["idempotency_scope_digest"]
    )


def downgrade() -> None:
    # 先恢复 M4 按名称引用的冲突目标；不兼容数据会让整个迁移事务回滚。
    op.create_unique_constraint(
        "uq_tasks_idempotency_scope",
        "tasks",
        ["tenant_id", "environment_id", "idempotency_key"],
    )
    op.drop_constraint("uq_tasks_idempotency_scope_digest", "tasks", type_="unique")
    op.drop_constraint("uq_tasks_created_seq", "tasks", type_="unique")
    op.drop_constraint("ck_tasks_task_failure_count_non_negative", "tasks", type_="check")
    op.drop_constraint("ck_tasks_attempt_number_non_negative", "tasks", type_="check")
    op.drop_constraint("ck_tasks_created_seq_positive", "tasks", type_="check")
    op.drop_column("tasks", "idempotency_scope_digest")
    op.drop_column("tasks", "next_attempt_at")
    op.drop_column("tasks", "task_failure_count")
    op.drop_column("tasks", "attempt_number")
    op.drop_column("tasks", "created_seq")
    op.execute(sa.schema.DropSequence(sa.Sequence(_CREATED_SEQUENCE)))

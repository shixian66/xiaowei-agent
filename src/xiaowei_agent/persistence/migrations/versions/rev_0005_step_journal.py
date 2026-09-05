"""Persist the atomic step execution journal.

Revision ID: 0005_step_journal
Revises: 0004_retry_markers
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0005_step_journal"
down_revision: str | None = "0004_retry_markers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEP_EXECUTIONS = sa.table(
    "task_step_executions", sa.column("task_id", sa.Text())
)


def upgrade() -> None:
    op.create_table(
        "task_step_executions",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column("last_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("result_status", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("evidence_id", sa.Text(), nullable=True),
        sa.Column("commit_digest", sa.CHAR(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("task_id", "step_id"),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_table("task_step_executions")
        return
    connection = op.get_bind()
    require_destructive_authorization(
        connection, guarded=((_STEP_EXECUTIONS, "task_step_executions"),)
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET status = 'failed', version = version + 1, "
            "terminal_reason = 'm5_downgrade_discarded' "
            "WHERE status IN ('created', 'planning', 'running', 'awaiting_approval') "
            "AND EXISTS (SELECT 1 FROM task_step_executions s "
            "WHERE s.task_id = tasks.task_id)"
        )
    )
    op.drop_table("task_step_executions")

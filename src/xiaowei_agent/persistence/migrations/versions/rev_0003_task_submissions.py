"""Persist immutable task submissions and settle M4 in-flight tasks.

Revision ID: 0003_task_submissions
Revises: 0002_task_execution_columns
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0003_task_submissions"
down_revision: str | None = "0002_task_execution_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBMISSIONS = sa.table("task_submissions", sa.column("task_id", sa.Text()))
def upgrade() -> None:
    op.create_table(
        "task_submissions",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("envelope", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submission_digest", sa.CHAR(length=64), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET status = 'failed', version = version + 1, "
            "terminal_reason = 'legacy_task_without_submission' "
            "WHERE status IN ('created', 'planning', 'running', 'awaiting_approval')"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS ("
            " SELECT 1 FROM tasks t LEFT JOIN task_submissions s ON s.task_id = t.task_id"
            " WHERE t.status IN ('created', 'planning', 'running', 'awaiting_approval')"
            " AND s.task_id IS NULL"
            ") THEN RAISE EXCEPTION 'M5_ACTIVE_TASK_WITHOUT_SUBMISSION'; END IF; END $$"
        )
    )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_table("task_submissions")
        return
    connection = op.get_bind()
    require_destructive_authorization(
        connection,
        guarded=((_SUBMISSIONS, "task_submissions"),),
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET status = 'failed', version = version + 1, "
            "terminal_reason = 'm5_downgrade_discarded' "
            "WHERE status IN ('created', 'planning', 'running', 'awaiting_approval') "
            "AND EXISTS (SELECT 1 FROM task_submissions s WHERE s.task_id = tasks.task_id)"
        )
    )
    op.drop_table("task_submissions")

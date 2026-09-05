"""Persist idempotent task-retry markers.

Revision ID: 0004_retry_markers
Revises: 0003_task_submissions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_retry_markers"
down_revision: str | None = "0003_task_submissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks", sa.Column("retry_scheduled_by_attempt", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "tasks", sa.Column("retry_command_digest", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_tasks_retry_markers_consistent",
        "tasks",
        "(retry_scheduled_by_attempt IS NULL AND retry_command_digest IS NULL)"
        " OR (retry_scheduled_by_attempt IS NOT NULL AND retry_command_digest IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_tasks_retry_attempt_not_future",
        "tasks",
        "retry_scheduled_by_attempt IS NULL"
        " OR (retry_scheduled_by_attempt > 0"
        " AND retry_scheduled_by_attempt <= attempt_number)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_retry_attempt_not_future", "tasks", type_="check")
    op.drop_constraint("ck_tasks_retry_markers_consistent", "tasks", type_="check")
    op.drop_column("tasks", "retry_command_digest")
    op.drop_column("tasks", "retry_scheduled_by_attempt")

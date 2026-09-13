"""Persist explicit task parent links without changing existing digest bytes.

Revision ID: 0009_task_parent_context
Revises: 0008_model_artifacts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0009_task_parent_context"
down_revision: str | None = "0008_model_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBMISSIONS = sa.table(
    "task_submissions",
    sa.column("parent_task_id", sa.Text()),
)
_PARENTED_SUBMISSIONS = (
    sa.select(_SUBMISSIONS.c.parent_task_id)
    .where(_SUBMISSIONS.c.parent_task_id.is_not(None))
    .subquery()
)


def upgrade() -> None:
    op.add_column(
        "task_submissions",
        sa.Column("parent_task_id", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_task_submissions_parent_task",
        "task_submissions",
        "tasks",
        ["parent_task_id"],
        ["task_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_task_submissions_parent_task_id",
        "task_submissions",
        ["parent_task_id"],
        unique=False,
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=((_PARENTED_SUBMISSIONS, "task_parent_context"),),
        )
    op.drop_index(
        "ix_task_submissions_parent_task_id",
        table_name="task_submissions",
    )
    op.drop_constraint(
        "fk_task_submissions_parent_task",
        "task_submissions",
        type_="foreignkey",
    )
    op.drop_column("task_submissions", "parent_task_id")

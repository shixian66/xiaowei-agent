"""Rename task parent links to clarification parent links.

Revision ID: 0013_clarification_parent
Revises: 0012_clarification_records
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
    require_no_rows,
)

revision: str = "0013_clarification_parent"
down_revision: str | None = "0012_clarification_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBMISSIONS = sa.table(
    "task_submissions",
    sa.column("parent_task_id", sa.Text()),
)
_LEGACY_PARENTED_SUBMISSIONS = (
    sa.select(_SUBMISSIONS.c.parent_task_id)
    .where(_SUBMISSIONS.c.parent_task_id.is_not(None))
    .subquery()
)
_HEAD_SUBMISSIONS = sa.table(
    "task_submissions",
    sa.column("clarification_parent_task_id", sa.Text()),
)
_CLARIFICATION_PARENTED_SUBMISSIONS = (
    sa.select(_HEAD_SUBMISSIONS.c.clarification_parent_task_id)
    .where(_HEAD_SUBMISSIONS.c.clarification_parent_task_id.is_not(None))
    .subquery()
)


def upgrade() -> None:
    if not context.is_offline_mode():
        require_no_rows(
            op.get_bind(),
            guarded=((_LEGACY_PARENTED_SUBMISSIONS, "legacy_task_parent_context"),),
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
    op.alter_column(
        "task_submissions",
        "parent_task_id",
        new_column_name="clarification_parent_task_id",
    )
    op.create_foreign_key(
        "fk_task_submissions_clarification_parent_task",
        "task_submissions",
        "tasks",
        ["clarification_parent_task_id"],
        ["task_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_task_submissions_clarification_parent_task_id",
        "task_submissions",
        ["clarification_parent_task_id"],
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=((_CLARIFICATION_PARENTED_SUBMISSIONS, "task_parent_context"),),
        )
    op.execute(
        sa.update(_HEAD_SUBMISSIONS)
        .where(_HEAD_SUBMISSIONS.c.clarification_parent_task_id.is_not(None))
        .values(clarification_parent_task_id=None)
    )
    op.drop_constraint(
        "uq_task_submissions_clarification_parent_task_id",
        "task_submissions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_task_submissions_clarification_parent_task",
        "task_submissions",
        type_="foreignkey",
    )
    op.alter_column(
        "task_submissions",
        "clarification_parent_task_id",
        new_column_name="parent_task_id",
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

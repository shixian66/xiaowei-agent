"""Persist clarification terminal records.

Revision ID: 0012_clarification_records
Revises: 0011_interaction_clarification
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0012_clarification_records"
down_revision: str | None = "0011_interaction_clarification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLARIFICATIONS = sa.table(
    "task_clarification_records",
    sa.column("task_id", sa.Text()),
)
_ANY_CLARIFICATIONS = sa.select(_CLARIFICATIONS.c.task_id).subquery()


def upgrade() -> None:
    op.create_table(
        "task_clarification_records",
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("subject", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("missing_fields", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_slots", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_clarification_records_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "record_version = 1",
            name="ck_task_clarification_records_version",
        ),
        sa.CheckConstraint(
            "fencing_token > 0",
            name="ck_task_clarification_records_fencing_positive",
        ),
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=((_ANY_CLARIFICATIONS, "task_clarification_records"),),
        )
    op.drop_table("task_clarification_records")

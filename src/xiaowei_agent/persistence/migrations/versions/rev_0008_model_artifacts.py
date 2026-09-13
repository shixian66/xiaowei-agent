"""Persist grant-fenced, insert-once model artifacts.

Revision ID: 0008_model_artifacts
Revises: 0007_web_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0008_model_artifacts"
down_revision: str | None = "0007_web_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTENTS = sa.table("task_accepted_intents", sa.column("task_id", sa.Text()))
_ADVISORIES = sa.table("task_model_advisories", sa.column("task_id", sa.Text()))


def upgrade() -> None:
    op.create_table(
        "task_accepted_intents",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("draft", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("provider_origin", sa.Text(), nullable=True),
        sa.Column("prompt_revision", sa.Text(), nullable=False),
        sa.Column("schema_revision", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("result_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("usage", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_accepted_intents_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "artifact_version = 1", name="ck_task_accepted_intents_version"
        ),
        sa.CheckConstraint(
            "fencing_token > 0", name="ck_task_accepted_intents_fencing_positive"
        ),
        sa.CheckConstraint(
            "(origin = 'model' AND provider IS NOT NULL AND model IS NOT NULL"
            " AND provider_origin IS NOT NULL) OR"
            " (origin = 'rule' AND provider IS NULL AND model IS NULL"
            " AND provider_origin IS NULL)",
            name="ck_task_accepted_intents_origin_identity",
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "task_model_advisories",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("advisory", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider_origin", sa.Text(), nullable=False),
        sa.Column("prompt_revision", sa.Text(), nullable=False),
        sa.Column("schema_revision", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("result_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("usage", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_model_advisories_task",
            ondelete="CASCADE",
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
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=(
                (_ADVISORIES, "task_model_advisories"),
                (_INTENTS, "task_accepted_intents"),
            ),
        )
    op.drop_table("task_model_advisories")
    op.drop_table("task_accepted_intents")

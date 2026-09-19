"""Rename accepted intent artifacts to interaction artifacts.

Revision ID: 0011_interaction_clarification
Revises: 0010_local_admin_provider
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0011_interaction_clarification"
down_revision: str | None = "0010_local_admin_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTERACTIONS = sa.table(
    "task_interaction_artifacts",
    sa.column("artifact_version", sa.Integer()),
)
_V2_INTERACTIONS = (
    sa.select(_INTERACTIONS.c.artifact_version)
    .where(_INTERACTIONS.c.artifact_version == 2)
    .subquery()
)
_CLARIFICATIONS = sa.table(
    "task_clarification_records",
    sa.column("task_id", sa.Text()),
)
_ANY_CLARIFICATIONS = sa.select(_CLARIFICATIONS.c.task_id).subquery()


def upgrade() -> None:
    op.rename_table("task_accepted_intents", "task_interaction_artifacts")
    op.drop_constraint(
        "fk_task_accepted_intents_task",
        "task_interaction_artifacts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_task_accepted_intents_version",
        "task_interaction_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_accepted_intents_fencing_positive",
        "task_interaction_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_accepted_intents_origin_identity",
        "task_interaction_artifacts",
        type_="check",
    )
    op.create_foreign_key(
        "fk_task_interaction_artifacts_task",
        "task_interaction_artifacts",
        "tasks",
        ["task_id"],
        ["task_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_task_interaction_artifacts_version",
        "task_interaction_artifacts",
        sa.text("artifact_version IN (1, 2)"),
    )
    op.create_check_constraint(
        "ck_task_interaction_artifacts_fencing_positive",
        "task_interaction_artifacts",
        sa.text("fencing_token > 0"),
    )
    op.create_check_constraint(
        "ck_task_interaction_artifacts_origin_identity",
        "task_interaction_artifacts",
        sa.text(
            "(origin = 'model' AND provider IS NOT NULL AND model IS NOT NULL"
            " AND provider_origin IS NOT NULL) OR"
            " (origin = 'rule' AND provider IS NULL AND model IS NULL"
            " AND provider_origin IS NULL)"
        ),
    )
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
            guarded=(
                (_ANY_CLARIFICATIONS, "task_clarification_records"),
                (_V2_INTERACTIONS, "task_interaction_artifacts_v2"),
            ),
        )
    op.drop_table("task_clarification_records")
    op.execute(
        sa.delete(_INTERACTIONS).where(_INTERACTIONS.c.artifact_version == 2)
    )
    op.drop_constraint(
        "fk_task_interaction_artifacts_task",
        "task_interaction_artifacts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_task_interaction_artifacts_version",
        "task_interaction_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_interaction_artifacts_fencing_positive",
        "task_interaction_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_interaction_artifacts_origin_identity",
        "task_interaction_artifacts",
        type_="check",
    )
    op.rename_table("task_interaction_artifacts", "task_accepted_intents")
    op.create_foreign_key(
        "fk_task_accepted_intents_task",
        "task_accepted_intents",
        "tasks",
        ["task_id"],
        ["task_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_task_accepted_intents_version",
        "task_accepted_intents",
        sa.text("artifact_version = 1"),
    )
    op.create_check_constraint(
        "ck_task_accepted_intents_fencing_positive",
        "task_accepted_intents",
        sa.text("fencing_token > 0"),
    )
    op.create_check_constraint(
        "ck_task_accepted_intents_origin_identity",
        "task_accepted_intents",
        sa.text(
            "(origin = 'model' AND provider IS NOT NULL AND model IS NOT NULL"
            " AND provider_origin IS NOT NULL) OR"
            " (origin = 'rule' AND provider IS NULL AND model IS NULL"
            " AND provider_origin IS NULL)"
        ),
    )

"""Rename accepted intent artifacts to interaction artifacts.

Revision ID: 0011_interaction_clarification
Revises: 0010_local_admin_provider
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_interaction_clarification"
down_revision: str | None = "0010_local_admin_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def downgrade() -> None:
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

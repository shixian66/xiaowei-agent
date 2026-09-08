"""Persist channel bindings and projection subscriptions.

Revision ID: 0006_channels
Revises: 0005_step_journal
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0006_channels"
down_revision: str | None = "0005_step_journal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FENCING_SEQUENCE = "projection_fencing_token_seq"
_BINDINGS = sa.table("channel_bindings", sa.column("binding_id", sa.Text()))
_SUBSCRIPTIONS = sa.table(
    "projection_subscriptions", sa.column("subscription_id", sa.Text())
)


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence(_FENCING_SEQUENCE, start=1)))
    op.create_table(
        "channel_bindings",
        sa.Column("binding_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("initiator_subject_ref", sa.Text(), nullable=False),
        sa.Column("conversation_ref", sa.Text(), nullable=True),
        sa.Column("source_event_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_channel_bindings_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint("task_id", name="uq_channel_bindings_task"),
        sa.UniqueConstraint(
            "tenant_id",
            "environment_id",
            "channel",
            "source_event_ref",
            name="uq_channel_bindings_source",
        ),
        sa.CheckConstraint(
            "channel IN ('feishu_private', 'feishu_group', 'web')",
            name="ck_channel_bindings_channel",
        ),
        sa.CheckConstraint(
            "channel != 'feishu_group' OR conversation_ref IS NOT NULL",
            name="ck_channel_bindings_group_conversation",
        ),
    )
    op.create_table(
        "projection_subscriptions",
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("destination_kind", sa.Text(), nullable=False),
        sa.Column("destination_ref", sa.Text(), nullable=False),
        sa.Column("source_message_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("last_projected_task_version", sa.BigInteger(), nullable=True),
        sa.Column("attempt_number", sa.BigInteger(), nullable=False),
        sa.Column("claim_owner", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_failure_count", sa.BigInteger(), nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("payload_digest", sa.CHAR(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_projection_subscriptions_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subscription_id"),
        sa.UniqueConstraint(
            "task_id",
            "destination_kind",
            "destination_ref",
            name="uq_projection_subscriptions_destination",
        ),
        sa.CheckConstraint(
            "destination_kind IN ('feishu_message_card', 'feishu_private_notice')",
            name="ck_projection_subscriptions_destination_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending_initial', 'waiting_terminal', 'delivering_terminal',"
            " 'completed', 'dead_letter')",
            name="ck_projection_subscriptions_state",
        ),
        sa.CheckConstraint(
            "attempt_number >= 0",
            name="ck_projection_subscriptions_attempt_non_negative",
        ),
        sa.CheckConstraint(
            "provider_failure_count >= 0",
            name="ck_projection_subscriptions_failure_non_negative",
        ),
        sa.CheckConstraint(
            "fencing_token IS NULL OR fencing_token > 0",
            name="ck_projection_subscriptions_fencing_positive",
        ),
        sa.CheckConstraint(
            "last_projected_task_version IS NULL OR last_projected_task_version >= 0",
            name="ck_projection_subscriptions_version_non_negative",
        ),
        sa.CheckConstraint(
            "(claim_owner IS NULL AND claim_expires_at IS NULL AND fencing_token IS NULL)"
            " OR (claim_owner IS NOT NULL AND claim_expires_at IS NOT NULL"
            " AND fencing_token IS NOT NULL)",
            name="ck_projection_subscriptions_claim_consistent",
        ),
        sa.CheckConstraint(
            "state NOT IN ('completed', 'dead_letter') OR"
            " (claim_owner IS NULL AND claim_expires_at IS NULL AND fencing_token IS NULL)",
            name="ck_projection_subscriptions_terminal_unclaimed",
        ),
        sa.CheckConstraint(
            "(last_projected_task_version IS NULL AND payload_digest IS NULL) OR"
            " (last_projected_task_version IS NOT NULL AND payload_digest IS NOT NULL)",
            name="ck_projection_subscriptions_projection_consistent",
        ),
        sa.CheckConstraint(
            "(provider_failure_count = 0 AND last_error_code IS NULL) OR"
            " (provider_failure_count > 0 AND last_error_code IS NOT NULL)",
            name="ck_projection_subscriptions_failure_consistent",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code IN"
            " ('provider_rate_limited', 'provider_timeout', 'provider_unavailable',"
            " 'provider_unauthorized', 'provider_forbidden', 'provider_invalid_payload',"
            " 'provider_internal')",
            name="ck_projection_subscriptions_error_code",
        ),
        sa.CheckConstraint(
            "state != 'completed' OR"
            " (source_message_ref IS NOT NULL AND last_projected_task_version IS NOT NULL"
            " AND payload_digest IS NOT NULL)",
            name="ck_projection_subscriptions_completed_payload",
        ),
    )
    op.create_index(
        "ix_projection_subscriptions_due",
        "projection_subscriptions",
        ["state", "next_attempt_at", "subscription_id"],
        unique=False,
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=(
                (_SUBSCRIPTIONS, "projection_subscriptions"),
                (_BINDINGS, "channel_bindings"),
            ),
        )
    op.drop_index(
        "ix_projection_subscriptions_due",
        table_name="projection_subscriptions",
    )
    op.drop_table("projection_subscriptions")
    op.drop_table("channel_bindings")
    op.execute(sa.schema.DropSequence(sa.Sequence(_FENCING_SEQUENCE)))

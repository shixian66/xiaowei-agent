"""Persist digest-only Web OAuth states and browser sessions.

Revision ID: 0007_web_sessions
Revises: 0006_channels
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0007_web_sessions"
down_revision: str | None = "0006_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OAUTH_STATES = sa.table(
    "web_oauth_states", sa.column("state_digest", sa.CHAR(length=64))
)
_SESSIONS = sa.table(
    "web_sessions", sa.column("session_digest", sa.CHAR(length=64))
)


def upgrade() -> None:
    op.create_table(
        "web_oauth_states",
        sa.Column("state_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("state_digest"),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_web_oauth_states_expiry_after_issue",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR"
            " (consumed_at >= issued_at AND consumed_at < expires_at)",
            name="ck_web_oauth_states_consumption_window",
        ),
    )
    op.create_table(
        "web_sessions",
        sa.Column("session_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("session_digest"),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_web_sessions_expiry_after_issue",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="ck_web_sessions_revocation_after_issue",
        ),
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=(
                (_SESSIONS, "web_sessions"),
                (_OAUTH_STATES, "web_oauth_states"),
            ),
        )
    op.drop_table("web_sessions")
    op.drop_table("web_oauth_states")

"""Add bounded W3 Admin query indexes.

Revision ID: 0017_w3_admin_query_indexes
Revises: 0016_web_login_contexts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_w3_admin_query_indexes"
down_revision: str | None = "0016_web_login_contexts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_user_role_assignments_scope_user",
        "user_role_assignments",
        ["tenant_id", "environment_id", "user_id"],
        unique=False,
    )
    op.create_index(
        "ix_activation_requests_scope_status_requested",
        "activation_requests",
        [
            "tenant_id",
            "environment_id",
            "status",
            sa.text("requested_at DESC"),
            sa.text("request_id DESC"),
        ],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_events_scope_created",
        "admin_audit_events",
        [
            "tenant_id",
            "environment_id",
            sa.text("created_at DESC"),
            sa.text("event_id DESC"),
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_audit_events_scope_created",
        table_name="admin_audit_events",
    )
    op.drop_index(
        "ix_activation_requests_scope_status_requested",
        table_name="activation_requests",
    )
    op.drop_index(
        "ix_user_role_assignments_scope_user",
        table_name="user_role_assignments",
    )

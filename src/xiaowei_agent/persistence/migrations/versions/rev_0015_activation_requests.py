"""Activation requests and W1b audit actions.

Revision ID: 0015_activation_requests
Revises: 0014_identity_admin_audit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError

revision: str = "0015_activation_requests"
down_revision: str | None = "0014_identity_admin_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVATION_REQUESTS = sa.table(
    "activation_requests", sa.column("request_id", sa.Text())
)
_ACTIVATION_AUDITS = sa.table(
    "admin_audit_events",
    sa.column("action", sa.Text()),
)
_ACTIVATION_ACTIONS = ("activation_approved", "activation_rejected")

_OLD_ACTIONS = (
    "action IN ('external_identity_bound', 'external_identity_unbound', "
    "'legacy_identity_migrated', 'local_admin_bootstrapped', 'role_assigned', "
    "'role_revoked', 'user_created', 'user_status_changed')"
)
_NEW_ACTIONS = (
    "action IN ('activation_approved', 'activation_rejected', "
    "'external_identity_bound', 'external_identity_unbound', "
    "'legacy_identity_migrated', 'local_admin_bootstrapped', 'role_assigned', "
    "'role_revoked', 'user_created', 'user_status_changed')"
)
_OLD_ROLE_ACTIONS = (
    "action IN ('legacy_identity_migrated', 'local_admin_bootstrapped', "
    "'role_assigned', 'user_created')"
)
_NEW_ROLE_ACTIONS = (
    "action IN ('activation_approved', 'legacy_identity_migrated', "
    "'local_admin_bootstrapped', 'role_assigned', 'user_created')"
)
_OLD_STATUS_ACTIONS = (
    "action IN ('legacy_identity_migrated', 'local_admin_bootstrapped', "
    "'user_created', 'user_status_changed')"
)
_NEW_STATUS_ACTIONS = (
    "action IN ('activation_approved', 'legacy_identity_migrated', "
    "'local_admin_bootstrapped', 'user_created', 'user_status_changed')"
)


def _replace_audit_constraints(*, new: bool) -> None:
    action_set = _NEW_ACTIONS if new else _OLD_ACTIONS
    role_set = _NEW_ROLE_ACTIONS if new else _OLD_ROLE_ACTIONS
    status_set = _NEW_STATUS_ACTIONS if new else _OLD_STATUS_ACTIONS
    for name in (
        "ck_admin_audit_events_action_closed",
        "ck_admin_audit_events_role_effect_required",
        "ck_admin_audit_events_status_effect_required",
        "ck_admin_audit_events_directory_actions_are_single_phase",
    ):
        op.drop_constraint(name, "admin_audit_events", type_="check")
    op.create_check_constraint(
        "ck_admin_audit_events_action_closed",
        "admin_audit_events",
        action_set,
    )
    op.create_check_constraint(
        "ck_admin_audit_events_role_effect_required",
        "admin_audit_events",
        f"outcome <> 'succeeded' OR ({role_set}) = (effect_role IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_admin_audit_events_status_effect_required",
        "admin_audit_events",
        f"outcome <> 'succeeded' OR ({status_set}) = (effect_status IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_admin_audit_events_directory_actions_are_single_phase",
        "admin_audit_events",
        f"outcome <> 'started' OR NOT ({action_set})",
    )


def upgrade() -> None:
    op.create_table(
        "activation_requests",
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        sa.Column("subject_ref_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("source_chat_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("approved_role", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
        sa.CheckConstraint(
            "provider IN ('feishu')",
            name="ck_activation_requests_provider_closed",
        ),
        sa.CheckConstraint(
            "source IN ('feishu_group', 'web_login')",
            name="ck_activation_requests_source_closed",
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'expired', 'pending', 'rejected')",
            name="ck_activation_requests_status_closed",
        ),
        sa.CheckConstraint(
            "approved_role IS NULL OR approved_role IN ('operator', 'user')",
            name="ck_activation_requests_approved_role_closed",
        ),
        sa.CheckConstraint(
            "expires_at > requested_at",
            name="ck_activation_requests_expiration_after_request",
        ),
        sa.CheckConstraint(
            "(source = 'web_login' AND source_event_digest IS NULL AND "
            "source_chat_digest IS NULL) OR (source = 'feishu_group' AND "
            "source_event_digest IS NOT NULL AND source_chat_digest IS NOT NULL)",
            name="ck_activation_requests_source_digests_match",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'expired') AND decided_at IS NULL AND "
            "decided_by IS NULL AND approved_role IS NULL) OR "
            "(status = 'rejected' AND decided_at IS NOT NULL AND decided_by IS "
            "NOT NULL AND approved_role IS NULL) OR (status = 'approved' AND "
            "decided_at IS NOT NULL AND decided_by IS NOT NULL AND "
            "approved_role IS NOT NULL)",
            name="ck_activation_requests_decision_fields_match_status",
        ),
    )
    op.create_index(
        "uq_activation_requests_pending_subject",
        "activation_requests",
        ["tenant_id", "environment_id", "provider", "subject_ref_digest"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    _replace_audit_constraints(new=True)


def downgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        request_count = int(
            connection.execute(
                sa.select(sa.func.count()).select_from(_ACTIVATION_REQUESTS)
            ).scalar_one()
        )
        audit_count = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(_ACTIVATION_AUDITS)
                .where(_ACTIVATION_AUDITS.c.action.in_(_ACTIVATION_ACTIONS))
            ).scalar_one()
        )
        counts = (
            ("activation_requests", request_count),
            ("activation_audit", audit_count),
        )
        destructive_allowed = (
            context.config.attributes.get("allow_destructive", False) is True
        )
        if audit_count or (request_count and not destructive_allowed):
            raise MigrationSafetyError(counts=counts)
    _replace_audit_constraints(new=False)
    op.drop_index(
        "uq_activation_requests_pending_subject",
        table_name="activation_requests",
    )
    op.drop_table("activation_requests")

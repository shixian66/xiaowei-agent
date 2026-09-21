"""Identity directory tables, admin audit events and the local admin account link.

Revision ID: 0014_identity_admin_audit
Revises: 0013_clarification_parent
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0014_identity_admin_audit"
down_revision: str | None = "0013_clarification_parent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 降级守卫按**表**计数。约束文本在这里写成字面量而不是从枚举生成：迁移是冻结的
# 历史快照，从活枚举生成会让一次枚举变更静默改写历史。两边今天必须逐字相等，
# 这由 tests/contract/test_schema_matches_migration.py 承重——将来改枚举时它会
# 转红，逼出一条新的 revision，而不是悄悄重写这一条。
_ACCOUNTS = sa.table("user_accounts", sa.column("user_id", sa.Text()))
_AUDIT_EVENTS = sa.table("admin_audit_events", sa.column("event_id", sa.Text()))


def upgrade() -> None:
    op.create_table(
        "user_accounts",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("actor", name="uq_user_accounts_actor"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_user_accounts_status_closed",
        ),
    )
    op.create_table(
        "user_role_assignments",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "tenant_id", "environment_id"),
        sa.CheckConstraint(
            "role IN ('admin', 'operator', 'user')",
            name="ck_user_role_assignments_role_closed",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_accounts.user_id"],
            name="fk_user_role_assignments_user_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "external_identities",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("subject_ref_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "provider", "tenant_id", "environment_id", "subject_ref_digest"
        ),
        sa.UniqueConstraint(
            "provider",
            "tenant_id",
            "environment_id",
            "user_id",
            name="uq_external_identities_one_subject_per_account",
        ),
        sa.CheckConstraint(
            "provider IN ('feishu')",
            name="ck_external_identities_provider_closed",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_accounts.user_id"],
            name="fk_external_identities_user_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "admin_audit_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("auth_source", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_ref_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("effect_role", sa.Text(), nullable=True),
        sa.Column("effect_status", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint(
            "auth_source IN ('feishu', 'local_admin')",
            name="ck_admin_audit_events_auth_source_closed",
        ),
        sa.CheckConstraint(
            "action IN ('external_identity_bound', 'external_identity_unbound', "
            "'legacy_identity_migrated', 'local_admin_bootstrapped', 'role_assigned', "
            "'role_revoked', 'user_created', 'user_status_changed')",
            name="ck_admin_audit_events_action_closed",
        ),
        sa.CheckConstraint(
            "target_kind IN ('activation', 'config', 'duty_binding', 'task_content', "
            "'user')",
            name="ck_admin_audit_events_target_kind_closed",
        ),
        sa.CheckConstraint(
            "outcome IN ('denied', 'failed', 'started', 'succeeded')",
            name="ck_admin_audit_events_outcome_closed",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('actor_not_admin', "
            "'audit_unwritable', 'auth_source_not_allowed', 'conflict', "
            "'scope_mismatch', 'target_not_found')",
            name="ck_admin_audit_events_reason_code_closed",
        ),
        sa.CheckConstraint(
            "effect_role IS NULL OR effect_role IN ('admin', 'operator', 'user')",
            name="ck_admin_audit_events_effect_role_closed",
        ),
        sa.CheckConstraint(
            "effect_status IS NULL OR effect_status IN ('active', 'disabled')",
            name="ck_admin_audit_events_effect_status_closed",
        ),
        sa.CheckConstraint(
            "(outcome IN ('denied', 'failed')) = (reason_code IS NOT NULL)",
            name="ck_admin_audit_events_reason_code_matches_outcome",
        ),
        sa.CheckConstraint(
            "outcome = 'succeeded' OR (effect_role IS NULL AND effect_status IS NULL)",
            name="ck_admin_audit_events_effect_only_on_success",
        ),
        sa.CheckConstraint(
            "outcome <> 'succeeded' OR (action IN ('legacy_identity_migrated', "
            "'local_admin_bootstrapped', 'role_assigned', 'user_created')) = "
            "(effect_role IS NOT NULL)",
            name="ck_admin_audit_events_role_effect_required",
        ),
        sa.CheckConstraint(
            "outcome <> 'succeeded' OR (action IN ('legacy_identity_migrated', "
            "'local_admin_bootstrapped', 'user_created', 'user_status_changed')) = "
            "(effect_status IS NOT NULL)",
            name="ck_admin_audit_events_status_effect_required",
        ),
        sa.CheckConstraint(
            "outcome <> 'started' OR NOT (action IN ('external_identity_bound', "
            "'external_identity_unbound', 'legacy_identity_migrated', "
            "'local_admin_bootstrapped', 'role_assigned', 'role_revoked', "
            "'user_created', 'user_status_changed'))",
            name="ck_admin_audit_events_directory_actions_are_single_phase",
        ),
    )
    op.create_index(
        "ix_admin_audit_events_created_at",
        "admin_audit_events",
        ["created_at"],
        unique=False,
    )
    # 两条**偏**唯一索引，不是给 operation_id 加全局 UNIQUE：规格 §14.2 的两阶段
    # 配置审计要求同一个 operation 写 STARTED 与终态两条事件。
    op.create_index(
        "uq_admin_audit_one_started_per_operation",
        "admin_audit_events",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("outcome = 'started'"),
    )
    op.create_index(
        "uq_admin_audit_one_terminal_per_operation",
        "admin_audit_events",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("outcome IN ('denied', 'failed', 'succeeded')"),
    )
    op.add_column(
        "local_admins",
        sa.Column("user_id", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_local_admins_user_id",
        "local_admins",
        "user_accounts",
        ["user_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=(
                (_ACCOUNTS, "identity_directory"),
                (_AUDIT_EVENTS, "admin_audit"),
            ),
        )
    op.drop_constraint("fk_local_admins_user_id", "local_admins", type_="foreignkey")
    op.drop_column("local_admins", "user_id")
    op.drop_table("admin_audit_events")
    op.drop_table("external_identities")
    op.drop_table("user_role_assignments")
    op.drop_table("user_accounts")

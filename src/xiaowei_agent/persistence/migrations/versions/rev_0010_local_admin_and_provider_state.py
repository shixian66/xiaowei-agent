"""Add local admin, provider state tables and bind sessions to their origin.

Revision ID: 0010_local_admin_provider
Revises: 0009_task_parent_context
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0010_local_admin_provider"
down_revision: str | None = "0009_task_parent_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCAL_ADMINS = sa.table("local_admins", sa.column("id", sa.SmallInteger))
_SERVICE_CONFIG_STATE = sa.table(
    "service_config_state", sa.column("service_name", sa.Text)
)
_PROVIDER_TEST_STATE = sa.table(
    "provider_test_state", sa.column("check_name", sa.Text)
)

# 旧 session 的哨兵 digest：认证时必然与任何真实 origin digest 不匹配，因此等价于
# 全体登出——与设计"切换模式等同于全体登出"一致，而不是让它们在新 origin 下续用。
_ORIGIN_DIGEST_SENTINEL = "0" * 64


def upgrade() -> None:
    op.create_table(
        "local_admins",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_local_admins_single_row"),
    )
    op.create_table(
        "service_config_state",
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("loaded_generation", sa.Integer(), nullable=False),
        sa.Column("load_status", sa.Text(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("service_name", "provider"),
        sa.CheckConstraint(
            "loaded_generation > 0",
            name="ck_service_config_state_generation_positive",
        ),
        sa.CheckConstraint(
            "load_status IN ('loaded', 'invalid')",
            name="ck_service_config_state_status_closed",
        ),
    )
    op.create_table(
        "provider_test_state",
        sa.Column("check_name", sa.Text(), nullable=False),
        sa.Column("tested_generation", sa.Integer(), nullable=False),
        sa.Column("test_status", sa.Text(), nullable=False),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("check_name"),
        sa.CheckConstraint(
            "check_name IN ('gemini_connection', 'feishu_credentials', 'feishu_oauth')",
            name="ck_provider_test_state_check_name_closed",
        ),
        sa.CheckConstraint(
            "tested_generation > 0",
            name="ck_provider_test_state_generation_positive",
        ),
        sa.CheckConstraint(
            "test_status IN ('passed', 'failed')",
            name="ck_provider_test_state_status_closed",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0 AND duration_ms <= 600000",
            name="ck_provider_test_state_duration_bounded",
        ),
        sa.CheckConstraint(
            "(test_status = 'passed') = (error_code IS NULL)",
            name="ck_provider_test_state_error_matches_status",
        ),
    )
    # 用 server_default 建好再摘掉：既有行在加非空列时必须有值，而摘掉默认值之后
    # 新行仍必须由调用方显式提供，不会悄悄回落到默认。
    op.add_column(
        "web_sessions",
        sa.Column(
            "auth_source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'feishu'"),
        ),
    )
    op.add_column(
        "web_sessions",
        sa.Column(
            "public_origin_digest",
            sa.CHAR(length=64),
            nullable=False,
            server_default=sa.text(f"'{_ORIGIN_DIGEST_SENTINEL}'"),
        ),
    )
    op.alter_column("web_sessions", "auth_source", server_default=None)
    op.alter_column("web_sessions", "public_origin_digest", server_default=None)
    op.create_check_constraint(
        "ck_web_sessions_auth_source_closed",
        "web_sessions",
        "auth_source IN ('local_admin', 'feishu')",
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=(
                (_PROVIDER_TEST_STATE, "provider_test_state"),
                (_SERVICE_CONFIG_STATE, "service_config_state"),
                (_LOCAL_ADMINS, "local_admins"),
            ),
        )
    op.drop_constraint(
        "ck_web_sessions_auth_source_closed", "web_sessions", type_="check"
    )
    op.drop_column("web_sessions", "public_origin_digest")
    op.drop_column("web_sessions", "auth_source")
    op.drop_table("provider_test_state")
    op.drop_table("service_config_state")
    op.drop_table("local_admins")

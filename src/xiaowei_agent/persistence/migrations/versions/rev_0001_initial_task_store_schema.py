"""M4 初始 schema：五张表 + fencing 序列。

Revision ID: 0001_initial
Revises:

**本文件不 import ``persistence/schema.py``。** 迁移是一份**冻结的历史快照**：它必须
永远描述"当时建的是什么"，而 schema 模块描述"现在应该是什么"。让迁移引用活的 schema
会使历史随代码一起漂移——两年后回放这条 revision，建出来的会是那时的表结构，而不是
当初的。两处必然重复，重复的对价是
``tests/contract/test_schema_matches_migration.py`` 离线比对两边的 DDL。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FENCING_SEQUENCE = "task_fencing_token_seq"


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence(_FENCING_SEQUENCE, start=1)))

    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("environment_id", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("request_digest", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("terminal_reason", sa.Text, nullable=True),
        sa.Column("lease_owner", sa.Text, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger, nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "environment_id",
            "idempotency_key",
            name="uq_tasks_idempotency_scope",
        ),
        sa.CheckConstraint("version >= 0", name="ck_tasks_version_non_negative"),
        sa.CheckConstraint(
            "fencing_token IS NULL OR fencing_token > 0",
            name="ck_tasks_fencing_token_positive",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL AND fencing_token IS NULL)"
            " OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND fencing_token IS NOT NULL)",
            name="ck_tasks_lease_fields_consistent",
        ),
    )

    op.create_table(
        "task_plans",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("target", JSONB, nullable=False),
    )

    op.create_table(
        "task_evidence",
        sa.Column("task_id", sa.Text, primary_key=True),
        sa.Column("evidence_id", sa.Text, primary_key=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("envelope", JSONB, nullable=False),
        sa.UniqueConstraint("task_id", "seq", name="uq_task_evidence_seq"),
    )

    op.create_table(
        "task_approvals",
        sa.Column("task_id", sa.Text, nullable=False),
        sa.Column("step_id", sa.Text, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("task_id", "seq", name="pk_task_approvals"),
    )

    op.create_table(
        "task_audit_events",
        sa.Column("task_id", sa.Text, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("task_id", "seq", name="pk_task_audit_events"),
    )


def downgrade() -> None:
    # 与 upgrade 严格逆序：先删引用方，再删被引用的序列。
    op.drop_table("task_audit_events")
    op.drop_table("task_approvals")
    op.drop_table("task_evidence")
    op.drop_table("task_plans")
    op.drop_table("tasks")
    op.execute(sa.schema.DropSequence(sa.Sequence(_FENCING_SEQUENCE)))

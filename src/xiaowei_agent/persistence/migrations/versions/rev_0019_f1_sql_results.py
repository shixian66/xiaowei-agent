"""F1 SQL artifacts, input_kind-discriminated submissions and result tables.

Revision ID: 0019_f1_sql_results
Revises: 0018_w4a_config_domains

ADR-018 D2, one migration on the same ``task_submissions`` table:

* ``input_kind`` is added with ``server_default='conversation'`` so every existing row
  is backfilled, and the default is then dropped; new rows must state their kind.
* ``envelope`` becomes nullable; ``sql_ref`` / ``sql_hash`` are added, and
  ``ck_task_submissions_shape`` pins the two row shapes. Conversation digests are
  untouched: their canonical inputs never included ``input_kind``.
* ``sql_artifacts`` (the only place SQL bytes live, purged to a tombstone),
  ``query_results`` and ``result_access_grants`` are created.

Downgrade refuses before any DDL, regardless of the destructive flag, while any SQL
submission, SQL artifact, result or grant exists: the old schema cannot represent
them. Otherwise it drops the new objects and restores ``envelope NOT NULL``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

from xiaowei_agent.persistence.migrations.guards import require_no_rows

revision: str = "0019_f1_sql_results"
down_revision: str | None = "0018_w4a_config_domains"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBMISSIONS = sa.table("task_submissions", sa.column("input_kind", sa.Text()))
_SQL_ARTIFACTS = sa.table("sql_artifacts", sa.column("sql_ref", sa.Text()))
_QUERY_RESULTS = sa.table("query_results", sa.column("result_ref", sa.Text()))
_GRANTS = sa.table("result_access_grants", sa.column("result_ref", sa.Text()))

_SHAPE = (
    "(input_kind = 'conversation' AND envelope IS NOT NULL "
    "AND sql_ref IS NULL AND sql_hash IS NULL) OR "
    "(input_kind = 'sql_artifact' AND envelope IS NULL "
    "AND clarification_parent_task_id IS NULL "
    "AND sql_ref IS NOT NULL AND sql_hash IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "sql_artifacts",
        sa.Column("sql_ref", sa.Text(), nullable=False),
        sa.Column("sql_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("sql_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("requester", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("sql_ref"),
        sa.CheckConstraint(
            "(purged_at IS NULL) = (sql_bytes IS NOT NULL)",
            name="ck_sql_artifacts_purge_shape",
        ),
        sa.CheckConstraint(
            "sql_bytes IS NULL OR octet_length(sql_bytes) BETWEEN 1 AND 65536",
            name="ck_sql_artifacts_sql_size",
        ),
    )
    op.create_index("ix_sql_artifacts_expires_at", "sql_artifacts", ["expires_at"])

    op.add_column(
        "task_submissions",
        sa.Column(
            "input_kind", sa.Text(), nullable=False, server_default="conversation"
        ),
    )
    op.alter_column("task_submissions", "input_kind", server_default=None)
    op.alter_column("task_submissions", "envelope", nullable=True)
    op.add_column("task_submissions", sa.Column("sql_ref", sa.Text(), nullable=True))
    op.add_column(
        "task_submissions", sa.Column("sql_hash", sa.CHAR(length=64), nullable=True)
    )
    op.create_foreign_key(
        "fk_task_submissions_sql_ref",
        "task_submissions",
        "sql_artifacts",
        ["sql_ref"],
        ["sql_ref"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_task_submissions_input_kind",
        "task_submissions",
        "input_kind IN ('conversation', 'sql_artifact')",
    )
    op.create_check_constraint("ck_task_submissions_shape", "task_submissions", _SHAPE)

    op.create_table(
        "query_results",
        sa.Column("result_ref", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("requester", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("target_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("config_revision", sa.Text(), nullable=False),
        sa.Column("sql_ref", sa.Text(), nullable=False),
        sa.Column("sql_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("query_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("columns", JSONB(), nullable=False),
        sa.Column("rows", JSONB(), nullable=False),
        sa.Column("saved_rows", sa.Integer(), nullable=False),
        sa.Column("saved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("has_more", sa.Boolean(), nullable=False),
        sa.Column("completeness", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("export_policy", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("result_ref"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_query_results_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sql_ref"],
            ["sql_artifacts.sql_ref"],
            name="fk_query_results_sql_ref",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("saved_rows BETWEEN 0 AND 1000", name="ck_query_results_saved_rows"),
        sa.CheckConstraint(
            "saved_bytes BETWEEN 0 AND 20971520", name="ck_query_results_saved_bytes"
        ),
        sa.CheckConstraint(
            "completeness IN ('complete', 'truncated_bytes', 'truncated_rows')",
            name="ck_query_results_completeness",
        ),
        sa.CheckConstraint(
            "export_policy = 'disabled'", name="ck_query_results_export_disabled"
        ),
    )
    op.create_index("ix_query_results_expires_at", "query_results", ["expires_at"])

    op.create_table(
        "result_access_grants",
        sa.Column("result_ref", sa.Text(), nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("grant_kind", sa.Text(), nullable=False),
        sa.Column("approval_ref", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("result_ref", "principal", "grant_kind"),
        sa.ForeignKeyConstraint(
            ["result_ref"],
            ["query_results.result_ref"],
            name="fk_result_access_grants_result",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "grant_kind IN ('export_approver', 'requester_owner', 'view_approver')",
            name="ck_result_access_grants_kind",
        ),
        sa.CheckConstraint(
            "grant_kind = 'requester_owner' OR approval_ref IS NOT NULL",
            name="ck_result_access_grants_approval_ref",
        ),
    )


def downgrade() -> None:
    # 离线 --sql 模式没有数据可查；真实降级一律先数行。
    if context.is_offline_mode():
        _drop_f1_objects()
        return
    require_no_rows(
        op.get_bind(),
        guarded=(
            (
                sa.select(_SUBMISSIONS.c.input_kind)
                .where(_SUBMISSIONS.c.input_kind == "sql_artifact")
                .subquery(),
                "sql_artifact_submissions",
            ),
            (_SQL_ARTIFACTS, "sql_artifacts"),
            (_QUERY_RESULTS, "query_results"),
            (_GRANTS, "result_access_grants"),
        ),
    )
    _drop_f1_objects()


def _drop_f1_objects() -> None:
    op.drop_table("result_access_grants")
    op.drop_index("ix_query_results_expires_at", table_name="query_results")
    op.drop_table("query_results")
    op.drop_constraint("ck_task_submissions_shape", "task_submissions", type_="check")
    op.drop_constraint(
        "ck_task_submissions_input_kind", "task_submissions", type_="check"
    )
    op.drop_constraint(
        "fk_task_submissions_sql_ref", "task_submissions", type_="foreignkey"
    )
    op.drop_column("task_submissions", "sql_hash")
    op.drop_column("task_submissions", "sql_ref")
    op.drop_column("task_submissions", "input_kind")
    op.alter_column("task_submissions", "envelope", nullable=False)
    op.drop_index("ix_sql_artifacts_expires_at", table_name="sql_artifacts")
    op.drop_table("sql_artifacts")

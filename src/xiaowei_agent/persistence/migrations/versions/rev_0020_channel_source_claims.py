"""Channel source events choose one submission kind before any task exists.

Revision ID: 0020_channel_source_claims
Revises: 0019_f1_sql_results

ADR-018 D2 keeps conversation and SQL idempotency keys in separate TaskStore scopes,
while a channel source event has exactly one ``source_event_ref``. Without a claim the
second kind of the same event creates a committed task, then loses the binding and is
left unbound but dispatchable. ``channel_source_claims`` is keyed by tenant, environment
and ``source_event_ref`` and records only the chosen ``input_kind``; the channel service
claims before creating the task, so the first kind wins atomically. ``source_event_ref``
is a digest over tenant, environment, channel, actor and event id, so channel needs no
column of its own.

Backfill covers every event that already has a task, not only bound ones: a task can be
committed while its binding failed, and the same event may be retried after the upgrade
under the new SQL recognizer. Candidates are every binding and every channel task, found
by its ``channel:v1:<source_event_ref>`` idempotency key (conversation rows must carry a
Web or Feishu envelope, since API callers may pick any key). For each event one
statement keeps the candidate with the smallest task ``created_seq`` (``DISTINCT ON``),
so the earliest task's kind wins whether that task is bound or not. Downgrade only drops the claims: they hold no user content and the
old schema needs none of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_channel_source_claims"
down_revision: str | None = "0019_f1_sql_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_source_claims",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("source_event_ref", sa.Text(), nullable=False),
        sa.Column("input_kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "environment_id",
            "source_event_ref",
            name="pk_channel_source_claims",
        ),
        sa.CheckConstraint(
            "input_kind IN ('conversation', 'sql_artifact')",
            name="ck_channel_source_claims_input_kind",
        ),
    )
    # 绑定与渠道任务合成一组候选，每个来源事件用 DISTINCT ON 取 created_seq 最早的任务：
    # 最早创建的任务的类型胜出，不因它是否已绑定而改变。
    op.execute(
        "INSERT INTO channel_source_claims "
        "(tenant_id, environment_id, source_event_ref, input_kind, created_at) "
        "SELECT DISTINCT ON (tenant_id, environment_id, source_event_ref) "
        "tenant_id, environment_id, source_event_ref, input_kind, created_at FROM ("
        "SELECT b.tenant_id, b.environment_id, b.source_event_ref, s.input_kind, "
        "b.created_at, t.created_seq "
        "FROM channel_bindings AS b "
        "JOIN tasks AS t ON t.task_id = b.task_id "
        "JOIN task_submissions AS s ON s.task_id = b.task_id "
        "UNION ALL "
        "SELECT t.tenant_id, t.environment_id, substr(t.idempotency_key, 12), "
        "s.input_kind, s.as_of, t.created_seq "
        "FROM tasks AS t JOIN task_submissions AS s ON s.task_id = t.task_id "
        "WHERE t.idempotency_key ~ '^channel:v1:[0-9a-f]{64}$' "
        "AND (s.input_kind = 'sql_artifact' "
        "OR s.envelope ->> 'channel' IN ('web', 'feishu'))"
        ") AS candidates "
        "ORDER BY tenant_id, environment_id, source_event_ref, created_seq"
    )


def downgrade() -> None:
    op.drop_table("channel_source_claims")

"""Closed Web login contexts and activation return intents.

Revision ID: 0016_web_login_contexts
Revises: 0015_activation_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import MigrationSafetyError

revision: str = "0016_web_login_contexts"
down_revision: str | None = "0015_activation_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OAUTH_STATES = sa.table(
    "web_oauth_states",
    sa.column("state_digest", sa.CHAR(64)),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("consumed_at", sa.DateTime(timezone=True)),
)
_LOGIN_CONTEXTS = sa.table(
    "web_oauth_login_contexts",
    sa.column("state_digest", sa.CHAR(64)),
)
_ACTIVATIONS = sa.table(
    "activation_requests",
    sa.column("source", sa.Text()),
    sa.column("return_intent_kind", sa.Text()),
    sa.column("return_intent_task_id", sa.Text()),
    sa.column("return_intent_request_id", sa.Text()),
)

_OLD_SOURCE_CLOSED = "source IN ('feishu_group', 'web_login')"
_NEW_SOURCE_CLOSED = "source IN ('feishu_group', 'safe_task_link', 'web_login')"
_OLD_SOURCE_DIGESTS = (
    "(source = 'web_login' AND source_event_digest IS NULL AND "
    "source_chat_digest IS NULL) OR (source = 'feishu_group' AND "
    "source_event_digest IS NOT NULL AND source_chat_digest IS NOT NULL)"
)
_NEW_SOURCE_DIGESTS = (
    "(source IN ('web_login', 'safe_task_link') AND source_event_digest IS NULL "
    "AND source_chat_digest IS NULL) OR (source = 'feishu_group' AND "
    "source_event_digest IS NOT NULL AND source_chat_digest IS NOT NULL)"
)
_RETURN_INTENT_SHAPE = (
    "(return_intent_kind IS NULL AND return_intent_task_id IS NULL AND "
    "return_intent_request_id IS NULL) OR (return_intent_kind IN "
    "('workbench', 'admin_center') AND return_intent_task_id IS NULL AND "
    "return_intent_request_id IS NULL) OR (return_intent_kind = "
    "'safe_task_detail' AND return_intent_task_id IS NOT NULL AND "
    "return_intent_request_id IS NULL) OR (return_intent_kind = "
    "'activation_status' AND return_intent_task_id IS NULL AND "
    "return_intent_request_id IS NOT NULL)"
)
_LOGIN_CONTEXT_SHAPE = (
    "(return_intent_kind IN ('workbench', 'admin_center') AND "
    "return_intent_task_id IS NULL AND return_intent_request_id IS NULL) OR "
    "(return_intent_kind = 'safe_task_detail' AND "
    "return_intent_task_id IS NOT NULL AND return_intent_request_id IS NULL) OR "
    "(return_intent_kind = 'activation_status' AND "
    "return_intent_task_id IS NULL AND return_intent_request_id IS NOT NULL)"
)
_SOURCE_INTENT_MATCH = (
    "(source = 'feishu_group' AND return_intent_kind IS NULL) OR "
    "(source = 'safe_task_link' AND return_intent_kind = 'safe_task_detail') OR "
    "(source = 'web_login' AND return_intent_kind IN "
    "('workbench', 'admin_center', 'activation_status'))"
)


def upgrade() -> None:
    # Existing tickets have no context and live for at most 600 seconds. Requiring a
    # retry is safer than silently treating an old connection-test state as login.
    op.execute(sa.delete(_OAUTH_STATES))
    op.create_table(
        "web_oauth_login_contexts",
        sa.Column("state_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("return_intent_kind", sa.Text(), nullable=False),
        sa.Column("return_intent_task_id", sa.Text(), nullable=True),
        sa.Column("return_intent_request_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("state_digest"),
        sa.ForeignKeyConstraint(
            ["state_digest"],
            ["web_oauth_states.state_digest"],
            name="fk_web_oauth_login_contexts_state_digest",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "return_intent_kind IN ('activation_status', 'admin_center', "
            "'safe_task_detail', 'workbench')",
            name="ck_web_oauth_login_contexts_kind_closed",
        ),
        sa.CheckConstraint(
            _LOGIN_CONTEXT_SHAPE,
            name="ck_web_oauth_login_contexts_intent_shape",
        ),
    )

    op.add_column(
        "activation_requests",
        sa.Column("return_intent_kind", sa.Text(), nullable=True),
    )
    op.add_column(
        "activation_requests",
        sa.Column("return_intent_task_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "activation_requests",
        sa.Column("return_intent_request_id", sa.Text(), nullable=True),
    )
    op.execute(
        sa.update(_ACTIVATIONS)
        .where(_ACTIVATIONS.c.source == "web_login")
        .values(return_intent_kind="workbench")
    )
    op.drop_constraint(
        "ck_activation_requests_source_closed",
        "activation_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_requests_source_digests_match",
        "activation_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_requests_source_closed",
        "activation_requests",
        _NEW_SOURCE_CLOSED,
    )
    op.create_check_constraint(
        "ck_activation_requests_source_digests_match",
        "activation_requests",
        _NEW_SOURCE_DIGESTS,
    )
    op.create_check_constraint(
        "ck_activation_requests_return_intent_shape",
        "activation_requests",
        _RETURN_INTENT_SHAPE,
    )
    op.create_check_constraint(
        "ck_activation_requests_source_intent_match",
        "activation_requests",
        _SOURCE_INTENT_MATCH,
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        active_contexts = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(
                    _LOGIN_CONTEXTS.join(
                        _OAUTH_STATES,
                        _LOGIN_CONTEXTS.c.state_digest
                        == _OAUTH_STATES.c.state_digest,
                    )
                )
                .where(
                    _OAUTH_STATES.c.consumed_at.is_(None),
                    _OAUTH_STATES.c.expires_at > sa.func.now(),
                )
            ).scalar_one()
        )
        safe_task_links = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(_ACTIVATIONS)
                .where(_ACTIVATIONS.c.source == "safe_task_link")
            ).scalar_one()
        )
        non_workbench_web = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(_ACTIVATIONS)
                .where(
                    _ACTIVATIONS.c.source == "web_login",
                    sa.or_(
                        _ACTIVATIONS.c.return_intent_kind != "workbench",
                        _ACTIVATIONS.c.return_intent_task_id.is_not(None),
                        _ACTIVATIONS.c.return_intent_request_id.is_not(None),
                    ),
                )
            ).scalar_one()
        )
        counts = (
            ("active_login_context", active_contexts),
            ("safe_task_link_activation", safe_task_links),
            ("non_workbench_web_activation", non_workbench_web),
        )
        if any(count for _, count in counts):
            raise MigrationSafetyError(counts=counts)

    op.drop_constraint(
        "ck_activation_requests_source_intent_match",
        "activation_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_requests_return_intent_shape",
        "activation_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_requests_source_digests_match",
        "activation_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_activation_requests_source_closed",
        "activation_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_activation_requests_source_closed",
        "activation_requests",
        _OLD_SOURCE_CLOSED,
    )
    op.create_check_constraint(
        "ck_activation_requests_source_digests_match",
        "activation_requests",
        _OLD_SOURCE_DIGESTS,
    )
    op.drop_column("activation_requests", "return_intent_request_id")
    op.drop_column("activation_requests", "return_intent_task_id")
    op.drop_column("activation_requests", "return_intent_kind")
    op.drop_table("web_oauth_login_contexts")

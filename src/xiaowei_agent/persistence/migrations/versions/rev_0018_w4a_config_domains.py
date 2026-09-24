"""W4a config domains, two-phase config audit actions and OAuth test contexts.

Revision ID: 0018_w4a_config_domains
Revises: 0017_w3_admin_query_indexes

* ``service_config_state.provider`` becomes ``config_domain``; legacy ``gemini``
  receipts map to ``ai`` and ``feishu`` stays ``feishu``. One closed CHECK admits
  ``ai/feishu/resources`` so W4b needs no further migration for the domain set.
* ``admin_audit_events`` admits the three startable config actions and the minimal
  closed failure reasons.
* ``web_oauth_test_contexts`` binds an OAuth connection-test state digest to its
  server-derived ``w4:`` audit operation id and to the Feishu config generation the
  Web process had loaded when the test started.

Downgrade refuses (before any DDL, regardless of the destructive flag) whenever W4a
audit facts, OAuth test contexts or ``resources`` receipts exist: the old schema
cannot represent them, and append-only audit is never deleted to make room.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from xiaowei_agent.persistence.migrations.guards import (
    MigrationPreconditionError,
    MigrationSafetyError,
)

revision: str = "0018_w4a_config_domains"
down_revision: str | None = "0017_w3_admin_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECEIPTS_OLD = sa.table("service_config_state", sa.column("provider", sa.Text()))
_RECEIPTS_NEW = sa.table("service_config_state", sa.column("config_domain", sa.Text()))
_AUDITS = sa.table(
    "admin_audit_events",
    sa.column("action", sa.Text()),
    sa.column("reason_code", sa.Text()),
)
_OAUTH_STATES = sa.table("web_oauth_states", sa.column("state_digest", sa.CHAR(64)))
_LOGIN_CONTEXTS = sa.table(
    "web_oauth_login_contexts", sa.column("state_digest", sa.CHAR(64))
)
_TEST_CONTEXTS = sa.table(
    "web_oauth_test_contexts", sa.column("state_digest", sa.CHAR(64))
)

_W4A_ACTIONS = ("config_cleared", "config_saved", "connection_tested")
_W4A_REASONS = ("config_invalid", "file_io_failed", "probe_failed", "session_invalid")

_OLD_ACTIONS = (
    "action IN ('activation_approved', 'activation_rejected', "
    "'external_identity_bound', 'external_identity_unbound', "
    "'legacy_identity_migrated', 'local_admin_bootstrapped', 'role_assigned', "
    "'role_revoked', 'user_created', 'user_status_changed')"
)
_NEW_ACTIONS = (
    "action IN ('activation_approved', 'activation_rejected', 'config_cleared', "
    "'config_saved', 'connection_tested', 'external_identity_bound', "
    "'external_identity_unbound', 'legacy_identity_migrated', "
    "'local_admin_bootstrapped', 'role_assigned', 'role_revoked', 'user_created', "
    "'user_status_changed')"
)
_OLD_REASONS = (
    "reason_code IS NULL OR reason_code IN ('actor_not_admin', "
    "'audit_unwritable', 'auth_source_not_allowed', 'conflict', "
    "'scope_mismatch', 'target_not_found')"
)
_NEW_REASONS = (
    "reason_code IS NULL OR reason_code IN ('actor_not_admin', "
    "'audit_unwritable', 'auth_source_not_allowed', 'config_invalid', 'conflict', "
    "'file_io_failed', 'probe_failed', 'scope_mismatch', 'session_invalid', "
    "'target_not_found')"
)
_DOMAIN_CLOSED = "config_domain IN ('ai', 'feishu', 'resources')"


def _replace_audit_checks(*, actions: str, reasons: str) -> None:
    for name in (
        "ck_admin_audit_events_action_closed",
        "ck_admin_audit_events_reason_code_closed",
    ):
        op.drop_constraint(name, "admin_audit_events", type_="check")
    op.create_check_constraint(
        "ck_admin_audit_events_action_closed", "admin_audit_events", actions
    )
    op.create_check_constraint(
        "ck_admin_audit_events_reason_code_closed", "admin_audit_events", reasons
    )


def upgrade() -> None:
    if not context.is_offline_mode():
        # 旧列只可能是 gemini/feishu；出现第三个值说明有人绕开了代码写库。
        # 那时静默映射会把它重解释成某个配置域，必须停下来。
        unknown = int(
            op.get_bind()
            .execute(
                sa.select(sa.func.count())
                .select_from(_RECEIPTS_OLD)
                .where(_RECEIPTS_OLD.c.provider.not_in(("gemini", "feishu")))
            )
            .scalar_one()
        )
        if unknown:
            raise MigrationPreconditionError(
                counts=(("unknown_receipt_provider", unknown),)
            )

    op.alter_column(
        "service_config_state", "provider", new_column_name="config_domain"
    )
    op.execute(
        sa.update(_RECEIPTS_NEW)
        .where(_RECEIPTS_NEW.c.config_domain == "gemini")
        .values(config_domain="ai")
    )
    op.create_check_constraint(
        "ck_service_config_state_domain_closed", "service_config_state", _DOMAIN_CLOSED
    )

    _replace_audit_checks(actions=_NEW_ACTIONS, reasons=_NEW_REASONS)

    # 旧连接测试 state 没有测试 context：升级后既不能被登录分支、也不能被测试分支
    # 消费。它们最多存活 600 秒，直接删掉比留一批永远无法兑现的票据更干净。
    op.execute(
        sa.delete(_OAUTH_STATES).where(
            _OAUTH_STATES.c.state_digest.not_in(sa.select(_LOGIN_CONTEXTS.c.state_digest))
        )
    )
    op.create_table(
        "web_oauth_test_contexts",
        sa.Column("state_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("config_generation", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("state_digest"),
        sa.ForeignKeyConstraint(
            ["state_digest"],
            ["web_oauth_states.state_digest"],
            name="fk_web_oauth_test_contexts_state_digest",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "operation_id", name="uq_web_oauth_test_contexts_operation_id"
        ),
        sa.CheckConstraint(
            "left(operation_id, 3) = 'w4:' AND char_length(operation_id) <= 64",
            name="ck_web_oauth_test_contexts_operation_id_shape",
        ),
        sa.CheckConstraint(
            "config_generation > 0",
            name="ck_web_oauth_test_contexts_config_generation_positive",
        ),
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        w4a_audits = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(_AUDITS)
                .where(
                    sa.or_(
                        _AUDITS.c.action.in_(_W4A_ACTIONS),
                        _AUDITS.c.reason_code.in_(_W4A_REASONS),
                    )
                )
            ).scalar_one()
        )
        test_contexts = int(
            connection.execute(
                sa.select(sa.func.count()).select_from(_TEST_CONTEXTS)
            ).scalar_one()
        )
        resources_receipts = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(_RECEIPTS_NEW)
                .where(_RECEIPTS_NEW.c.config_domain == "resources")
            ).scalar_one()
        )
        counts = (
            ("w4a_admin_audit_event", w4a_audits),
            ("oauth_test_context", test_contexts),
            ("resources_load_receipt", resources_receipts),
        )
        # 不看 allow_destructive：append-only 审计不为降级让路，resources 回执在旧
        # schema 里根本无法表达——任一存在都在任何 DDL 之前闭集拒绝。
        if any(count for _, count in counts):
            raise MigrationSafetyError(counts=counts)

    op.drop_table("web_oauth_test_contexts")
    _replace_audit_checks(actions=_OLD_ACTIONS, reasons=_OLD_REASONS)
    op.drop_constraint(
        "ck_service_config_state_domain_closed", "service_config_state", type_="check"
    )
    op.execute(
        sa.update(_RECEIPTS_NEW)
        .where(_RECEIPTS_NEW.c.config_domain == "ai")
        .values(config_domain="gemini")
    )
    op.alter_column(
        "service_config_state", "config_domain", new_column_name="provider"
    )
